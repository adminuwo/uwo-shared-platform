"""Secure provider execution orchestration."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from threading import RLock
from typing import Protocol

from packages.contracts import Product
from services.data_service_common import (
    EventRecorder,
    InMemoryOutbox,
    OutboxRecord,
    OutboxStatus,
    RepositoryIntegrityError,
    deterministic_id,
    platform_event,
)
from services.platform_billing.gateway import GatewayBilling

from .audit import AuditSink, audit_event
from .auth import VerifiedIdentity
from .authorization import EntitlementAuthorizer
from .billing import AuthorizationOnlyGatewayBilling, BillingAuthorizer, BillingCompensationError, BillingError
from .content_safety import ContentSafetyAuthorizer, ContentSafetyError
from .providers import ProviderError, ProviderRequest, ProviderUsage
from .resilience import ExecutionResult, ResilientProviderExecutor
from .router import ModelRouter, RouteRequest


@dataclass(frozen=True)
class SecureExecutionRequest:
    request_id: str
    tenant_id: str
    product: Product
    model: str
    region: str
    prompt: str


@dataclass(frozen=True)
class SecureExecutionResult:
    request_id: str
    provider: str
    model: str
    region: str
    output_text: str
    provider_request_id: str | None


@dataclass(frozen=True)
class ExecutionOutcome:
    """Durable execution state used to resume billing and mandatory events."""

    fingerprint: str
    result: ExecutionResult | None = None
    captured: bool = False
    success_event_enqueued: bool = False
    failure_code: str | None = None
    failure_kind: str | None = None
    provider_id: str | None = None
    failure_event_enqueued: bool = False
    compensation_failed: bool = False
    compensation_failure_event_enqueued: bool = False

    def __post_init__(self) -> None:
        if (self.result is None) == (self.failure_code is None):
            raise ValueError("execution outcome must contain exactly one provider result or failure")
        if self.result is None and (self.captured or self.success_event_enqueued):
            raise ValueError("failed execution cannot be captured or carry a success event")
        if self.result is not None and (self.failure_kind is not None or self.failure_event_enqueued):
            raise ValueError("successful provider result cannot carry provider-failure state")
        if self.captured and not self.success_event_enqueued:
            raise ValueError("captured execution must durably enqueue its success event")
        if self.compensation_failure_event_enqueued and not self.compensation_failed:
            raise ValueError("compensation event requires a compensation failure")


class ExecutionOutcomeRepository(Protocol):
    def get(self, tenant_id: str, request_id: str) -> ExecutionOutcome | None: ...

    def put(
        self,
        tenant_id: str,
        request_id: str,
        value: ExecutionOutcome,
        expected: ExecutionOutcome | None,
    ) -> ExecutionOutcome: ...


class ExecutionEventOutbox(Protocol):
    def get(self, record_id: str) -> OutboxRecord | None: ...

    def enqueue(self, record: OutboxRecord) -> OutboxRecord: ...


class ExecutionOutcomeUnitOfWork(Protocol):
    outcomes: ExecutionOutcomeRepository
    outbox: ExecutionEventOutbox

    def __enter__(self) -> "ExecutionOutcomeUnitOfWork": ...

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None: ...

    def commit(self) -> None: ...


class ExecutionOutcomeUnitOfWorkFactory(Protocol):
    def __call__(self) -> ExecutionOutcomeUnitOfWork: ...


# Compatibility name retained for dependency injection call sites.
ExecutionOutcomeStore = ExecutionOutcomeUnitOfWorkFactory


class _OutcomeOutbox(InMemoryOutbox):
    def __init__(self, state: "InMemoryExecutionOutcomeStore") -> None:
        super().__init__()
        self._state = state

    def enqueue(self, record: OutboxRecord) -> OutboxRecord:
        self._state._fail("outbox")
        return super().enqueue(record)


class _OutcomeRepository:
    def __init__(self, state: "InMemoryExecutionOutcomeStore") -> None:
        self._state = state

    def get(self, tenant_id: str, request_id: str) -> ExecutionOutcome | None:
        return self._state._records.get((tenant_id, request_id))

    def put(
        self,
        tenant_id: str,
        request_id: str,
        value: ExecutionOutcome,
        expected: ExecutionOutcome | None,
    ) -> ExecutionOutcome:
        self._state._fail("outcome")
        key = (tenant_id, request_id)
        current = self._state._records.get(key)
        if current != expected:
            raise BillingError("stale_execution_outcome", "execution outcome changed concurrently")
        self._state._records[key] = value
        return value


class _InMemoryExecutionOutcomeUnitOfWork:
    def __init__(self, state: "InMemoryExecutionOutcomeStore") -> None:
        self._state = state
        self.outcomes = _OutcomeRepository(state)
        self.outbox = state.outbox
        self._committed = False

    def __enter__(self) -> "_InMemoryExecutionOutcomeUnitOfWork":
        self._state._lock.acquire()
        self._snapshot = (dict(self._state._records), self.outbox.snapshot())
        return self

    def commit(self) -> None:
        self._committed = True

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if not self._committed:
            records, outbox = self._snapshot
            self._state._records = records
            self.outbox.restore(outbox)
        self._state._lock.release()


class InMemoryExecutionOutcomeStore:
    """Rollback-capable test UoW; production must inject durable storage."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._records: dict[tuple[str, str], ExecutionOutcome] = {}
        self.fail_next: str | None = None
        self.outbox = _OutcomeOutbox(self)

    def _fail(self, operation: str) -> None:
        if self.fail_next == operation:
            self.fail_next = None
            raise RepositoryIntegrityError(f"injected {operation} failure")

    def __call__(self) -> _InMemoryExecutionOutcomeUnitOfWork:
        return _InMemoryExecutionOutcomeUnitOfWork(self)

    def get(self, tenant_id: str, request_id: str) -> ExecutionOutcome | None:
        with self._lock:
            return self._records.get((tenant_id, request_id))


class SecureExecutionService:
    def __init__(
        self,
        router: ModelRouter,
        entitlements: EntitlementAuthorizer,
        billing: BillingAuthorizer,
        content_safety: ContentSafetyAuthorizer,
        providers: ResilientProviderExecutor,
        audit: AuditSink,
        gateway_billing: GatewayBilling | None = None,
        event_recorder: EventRecorder | None = None,
        outcomes: ExecutionOutcomeUnitOfWorkFactory | None = None,
    ) -> None:
        self._router = router
        self._entitlements = entitlements
        self._billing = billing
        self._content_safety = content_safety
        self._providers = providers
        self._audit = audit
        self._gateway_billing = gateway_billing or AuthorizationOnlyGatewayBilling(billing)
        # The recorder is optional telemetry only. Mandatory execution events
        # are written exclusively through the outcome transaction's outbox.
        self._telemetry = event_recorder
        self._outcome_uow = outcomes or InMemoryExecutionOutcomeStore()

    @staticmethod
    def _fingerprint(request: SecureExecutionRequest, actor_subject: str) -> str:
        value = {
            "actor_subject": actor_subject,
            "tenant_id": request.tenant_id,
            "product": request.product.value,
            "model": request.model,
            "region": request.region,
            "prompt": request.prompt,
        }
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _public_result(request: SecureExecutionRequest, result: ExecutionResult) -> SecureExecutionResult:
        return SecureExecutionResult(
            request_id=request.request_id,
            provider=result.provider_id,
            model=request.model,
            region=request.region,
            output_text=result.response.output_text,
            provider_request_id=result.response.provider_request_id,
        )

    @staticmethod
    def _event(
        event_type: str,
        request: SecureExecutionRequest,
        *,
        provider_id: str,
        reason_code: str | None = None,
    ):
        attributes = {
            "product": request.product.value,
            "region": request.region,
            "provider_id": provider_id,
        }
        if reason_code is not None:
            attributes["reason_code"] = reason_code
        return platform_event(event_type, request.tenant_id, request.request_id, attributes)

    @classmethod
    def _enqueue_event(
        cls,
        tx: ExecutionOutcomeUnitOfWork,
        event_type: str,
        request: SecureExecutionRequest,
        *,
        provider_id: str,
        reason_code: str | None = None,
    ) -> None:
        event = cls._event(
            event_type,
            request,
            provider_id=provider_id,
            reason_code=reason_code,
        )
        record_id = deterministic_id("outbox", event.event_id)
        existing = tx.outbox.get(record_id)
        if existing is not None:
            if existing.event.event_id != event.event_id or existing.event.event_type != event_type:
                raise BillingError("execution_event_conflict", "mandatory execution event has conflicting content")
            return
        tx.outbox.enqueue(OutboxRecord(record_id, event, OutboxStatus.PENDING, 0, None, 1))

    def _load_outcome(self, tenant_id: str, request_id: str) -> ExecutionOutcome | None:
        with self._outcome_uow() as tx:
            value = tx.outcomes.get(tenant_id, request_id)
            tx.commit()
            return value

    @staticmethod
    def _require_fingerprint(outcome: ExecutionOutcome, fingerprint: str) -> None:
        if outcome.fingerprint != fingerprint:
            raise BillingError("idempotency_conflict", "request ID was reused with different execution input")

    def _remember_provider_result(
        self,
        request: SecureExecutionRequest,
        fingerprint: str,
        result: ExecutionResult,
    ) -> ExecutionOutcome:
        with self._outcome_uow() as tx:
            existing = tx.outcomes.get(request.tenant_id, request.request_id)
            if existing is not None:
                self._require_fingerprint(existing, fingerprint)
                if existing.result != result:
                    raise BillingError("idempotency_conflict", "request ID identifies a different provider result")
                tx.commit()
                return existing
            value = ExecutionOutcome(fingerprint=fingerprint, result=result, provider_id=result.provider_id)
            tx.outcomes.put(request.tenant_id, request.request_id, value, None)
            tx.commit()
            return value

    @staticmethod
    def _failure_kind(error: Exception) -> str:
        return "content_safety" if isinstance(error, ContentSafetyError) else "provider"

    def _remember_failure(
        self,
        request: SecureExecutionRequest,
        fingerprint: str,
        provider_id: str,
        error: Exception,
    ) -> ExecutionOutcome:
        code = getattr(error, "code", "provider_failure")
        with self._outcome_uow() as tx:
            existing = tx.outcomes.get(request.tenant_id, request.request_id)
            if existing is not None:
                self._require_fingerprint(existing, fingerprint)
                if existing.result is not None or existing.failure_code != code:
                    raise BillingError("idempotency_conflict", "request ID identifies a different execution outcome")
                value = existing
            else:
                value = ExecutionOutcome(
                    fingerprint=fingerprint,
                    failure_code=code,
                    failure_kind=self._failure_kind(error),
                    provider_id=provider_id,
                )
            self._enqueue_event(
                tx,
                "provider.execution.failed",
                request,
                provider_id=provider_id,
                reason_code=code,
            )
            updated = replace(value, failure_event_enqueued=True)
            if updated != existing:
                tx.outcomes.put(request.tenant_id, request.request_id, updated, existing)
            tx.commit()
            return updated

    def _mark_captured_and_enqueue_success(
        self,
        request: SecureExecutionRequest,
        fingerprint: str,
        result: ExecutionResult,
    ) -> ExecutionOutcome:
        with self._outcome_uow() as tx:
            existing = tx.outcomes.get(request.tenant_id, request.request_id)
            if existing is None:
                raise BillingError("execution_outcome_missing", "provider outcome is unavailable")
            self._require_fingerprint(existing, fingerprint)
            if existing.result != result:
                raise BillingError("idempotency_conflict", "request ID identifies a different provider result")
            self._enqueue_event(
                tx,
                "provider.execution.succeeded",
                request,
                provider_id=result.provider_id,
            )
            updated = replace(existing, captured=True, success_event_enqueued=True)
            if updated != existing:
                tx.outcomes.put(request.tenant_id, request.request_id, updated, existing)
            tx.commit()
            return updated

    def _mark_compensation_failure(
        self,
        request: SecureExecutionRequest,
        fingerprint: str,
    ) -> ExecutionOutcome:
        with self._outcome_uow() as tx:
            existing = tx.outcomes.get(request.tenant_id, request.request_id)
            if existing is None:
                raise BillingError("execution_outcome_missing", "execution failure outcome is unavailable")
            self._require_fingerprint(existing, fingerprint)
            provider_id = existing.provider_id or "unknown-provider"
            self._enqueue_event(
                tx,
                "billing.reservation-compensation-failed",
                request,
                provider_id=provider_id,
                reason_code="billing_compensation_failed",
            )
            updated = replace(
                existing,
                compensation_failed=True,
                compensation_failure_event_enqueued=True,
            )
            if updated != existing:
                tx.outcomes.put(request.tenant_id, request.request_id, updated, existing)
            tx.commit()
            return updated

    def _ensure_mandatory_events(
        self,
        request: SecureExecutionRequest,
        fingerprint: str,
        outcome: ExecutionOutcome,
    ) -> ExecutionOutcome:
        with self._outcome_uow() as tx:
            current = tx.outcomes.get(request.tenant_id, request.request_id)
            if current is None:
                raise BillingError("execution_outcome_missing", "execution outcome is unavailable")
            self._require_fingerprint(current, fingerprint)
            updated = current
            provider_id = current.provider_id or (current.result.provider_id if current.result else "unknown-provider")
            if current.captured:
                self._enqueue_event(
                    tx,
                    "provider.execution.succeeded",
                    request,
                    provider_id=provider_id,
                )
                updated = replace(updated, success_event_enqueued=True)
            if current.failure_code is not None:
                self._enqueue_event(
                    tx,
                    "provider.execution.failed",
                    request,
                    provider_id=provider_id,
                    reason_code=current.failure_code,
                )
                updated = replace(updated, failure_event_enqueued=True)
            if current.compensation_failed:
                self._enqueue_event(
                    tx,
                    "billing.reservation-compensation-failed",
                    request,
                    provider_id=provider_id,
                    reason_code="billing_compensation_failed",
                )
                updated = replace(updated, compensation_failure_event_enqueued=True)
            if updated != current:
                tx.outcomes.put(request.tenant_id, request.request_id, updated, current)
            tx.commit()
            return updated

    def _compensate(
        self,
        reservation,
        request: SecureExecutionRequest,
        fingerprint: str,
        original: Exception,
    ) -> None:
        try:
            self._gateway_billing.release_on_failure(reservation)
        except Exception as compensation_error:
            self._audit.emit(
                audit_event(
                    "billing-compensation-failed",
                    request.request_id,
                    "failed",
                    tenant_id=request.tenant_id,
                    reason_code="billing_compensation_failed",
                )
            )
            self._mark_compensation_failure(request, fingerprint)
            error = BillingCompensationError()
            error.original_failure = original
            error.compensation_failure = compensation_error
            raise error from original

    @staticmethod
    def _raise_stored_failure(outcome: ExecutionOutcome) -> None:
        if outcome.failure_kind == "content_safety":
            raise ContentSafetyError(
                outcome.failure_code or "content_safety_denied",
                "stored output safety decision denied execution",
            )
        raise ProviderError(
            "stored provider execution failed",
            fallback_allowed=False,
            code=outcome.failure_code or "provider_failure",
        )

    def authorize_route(self, identity: VerifiedIdentity, request: RouteRequest, request_id: str) -> None:
        self._entitlements.authorize(identity, request.tenant_id, request.product, request.model)
        self._audit.emit(
            audit_event(
                "route.authorization",
                request_id,
                "allowed",
                tenant_id=identity.tenant_id,
                subject=identity.subject,
                product=request.product.value,
                model=request.model,
            )
        )

    def execute(self, identity: VerifiedIdentity, request: SecureExecutionRequest) -> SecureExecutionResult:
        self._entitlements.authorize(identity, request.tenant_id, request.product, request.model)
        fingerprint = self._fingerprint(request, identity.subject)
        stored = self._load_outcome(request.tenant_id, request.request_id)
        if stored is not None:
            self._require_fingerprint(stored, fingerprint)
            stored = self._ensure_mandatory_events(request, fingerprint, stored)
            if stored.captured:
                if stored.result is None:
                    raise BillingError("execution_outcome_invalid", "captured provider result is unavailable")
                return self._public_result(request, stored.result)

        self._gateway_billing.authorize_estimated_charge(
            identity.tenant_id,
            request.product,
            request.model,
            request.request_id,
        )
        self._content_safety.authorize_input(
            identity.tenant_id,
            request.product,
            request.model,
            request.prompt,
            request.request_id,
        )
        route = self._router.route(
            RouteRequest(request.tenant_id, request.product, request.model, request.region)
        )
        reservation = self._gateway_billing.reserve(
            identity.tenant_id,
            request.product,
            request.model,
            request.request_id,
        )

        if stored is not None and stored.failure_code is not None:
            original = ProviderError(
                "stored execution failure",
                fallback_allowed=False,
                code=stored.failure_code,
            )
            self._compensate(reservation, request, fingerprint, original)
            self._raise_stored_failure(stored)

        result = stored.result if stored is not None else None
        if result is None:
            self._audit.emit(
                audit_event(
                    "provider.execution",
                    request.request_id,
                    "started",
                    tenant_id=identity.tenant_id,
                    subject=identity.subject,
                    product=request.product.value,
                    model=request.model,
                    provider=route.provider,
                )
            )
            try:
                result = self._providers.execute(
                    (route.provider,) + route.fallback,
                    ProviderRequest(
                        request.request_id,
                        identity.tenant_id,
                        request.model,
                        request.prompt,
                    ),
                )
                if result.response.usage is None:
                    raise ProviderError(
                        "provider usage is missing",
                        fallback_allowed=False,
                        code="missing_usage",
                        provider_response_id=result.response.provider_request_id,
                    )
                if not isinstance(result.response.usage, ProviderUsage):
                    raise ProviderError(
                        "provider usage is malformed",
                        fallback_allowed=False,
                        code="malformed_response",
                        provider_response_id=result.response.provider_request_id,
                    )
                self._content_safety.authorize_output(
                    identity.tenant_id,
                    request.product,
                    request.model,
                    result.response.output_text,
                    request.request_id,
                )
            except Exception as original:
                self._remember_failure(request, fingerprint, route.provider, original)
                self._compensate(reservation, request, fingerprint, original)
                raise
            stored = self._remember_provider_result(request, fingerprint, result)

        usage = result.response.usage
        if usage is None or not isinstance(usage, ProviderUsage):
            code = "missing_usage" if usage is None else "malformed_response"
            original = ProviderError(
                "provider usage is unavailable",
                fallback_allowed=False,
                code=code,
                provider_response_id=result.response.provider_request_id,
            )
            self._remember_failure(request, fingerprint, route.provider, original)
            self._compensate(reservation, request, fingerprint, original)
            raise original

        self._gateway_billing.capture(
            reservation,
            result.provider_id,
            result.response.provider_model_id,
            request.region,
            result.response.provider_request_id,
            usage.input_tokens,
            usage.output_tokens,
            usage.total_tokens,
        )
        self._mark_captured_and_enqueue_success(request, fingerprint, result)
        self._audit.emit(
            audit_event(
                "provider.execution",
                request.request_id,
                "succeeded",
                tenant_id=identity.tenant_id,
                subject=identity.subject,
                product=request.product.value,
                model=request.model,
                provider=result.provider_id,
            )
        )
        return self._public_result(request, result)
