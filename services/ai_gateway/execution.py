"""Secure provider execution orchestration."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from threading import Lock

from packages.contracts import Product

from .audit import AuditSink, audit_event
from .auth import VerifiedIdentity
from .authorization import EntitlementAuthorizer
from .billing import AuthorizationOnlyGatewayBilling, BillingAuthorizer, BillingCompensationError, BillingError
from .content_safety import ContentSafetyAuthorizer
from .providers import ProviderError, ProviderRequest, ProviderUsage
from .resilience import ExecutionResult, ResilientProviderExecutor
from .router import ModelRouter, RouteRequest
from services.platform_billing.gateway import GatewayBilling


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


class SecureExecutionService:
    def __init__(self, router: ModelRouter, entitlements: EntitlementAuthorizer, billing: BillingAuthorizer, content_safety: ContentSafetyAuthorizer, providers: ResilientProviderExecutor, audit: AuditSink, gateway_billing: GatewayBilling | None = None) -> None:
        self._router = router
        self._entitlements = entitlements
        self._billing = billing
        self._content_safety = content_safety
        self._providers = providers
        self._audit = audit
        self._gateway_billing = gateway_billing or AuthorizationOnlyGatewayBilling(billing)
        self._pending_lock = Lock()
        self._pending_captures: dict[tuple[str, str], tuple[str, ExecutionResult]] = {}

    @staticmethod
    def _fingerprint(request: SecureExecutionRequest) -> str:
        value = {
            "tenant_id": request.tenant_id,
            "product": request.product.value,
            "model": request.model,
            "region": request.region,
            "prompt": request.prompt,
        }
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _pending(self, request: SecureExecutionRequest) -> ExecutionResult | None:
        key = (request.tenant_id, request.request_id)
        fingerprint = self._fingerprint(request)
        with self._pending_lock:
            pending = self._pending_captures.get(key)
            if pending is None:
                return None
            if pending[0] != fingerprint:
                raise BillingError("idempotency_conflict", "request ID was reused with different execution input")
            return pending[1]

    def _remember_pending(self, request: SecureExecutionRequest, result: ExecutionResult) -> None:
        with self._pending_lock:
            self._pending_captures[(request.tenant_id, request.request_id)] = (self._fingerprint(request), result)

    def _clear_pending(self, request: SecureExecutionRequest) -> None:
        with self._pending_lock:
            self._pending_captures.pop((request.tenant_id, request.request_id), None)

    def _compensate(self, reservation, request: SecureExecutionRequest, original: Exception) -> None:
        try:
            self._gateway_billing.release_on_failure(reservation)
        except Exception as compensation_error:
            self._audit.emit(audit_event("billing-compensation-failed", request.request_id, "failed", tenant_id=request.tenant_id, reason_code="billing_compensation_failed"))
            error = BillingCompensationError()
            error.original_failure = original
            error.compensation_failure = compensation_error
            raise error from original

    def authorize_route(self, identity: VerifiedIdentity, request: RouteRequest, request_id: str) -> None:
        self._entitlements.authorize(identity, request.tenant_id, request.product, request.model)
        self._audit.emit(audit_event("route.authorization", request_id, "allowed", tenant_id=identity.tenant_id, subject=identity.subject, product=request.product.value, model=request.model))

    def execute(self, identity: VerifiedIdentity, request: SecureExecutionRequest) -> SecureExecutionResult:
        self._entitlements.authorize(identity, request.tenant_id, request.product, request.model)
        self._gateway_billing.authorize_estimated_charge(identity.tenant_id, request.product, request.model, request.request_id)
        self._content_safety.authorize_input(identity.tenant_id, request.product, request.model, request.prompt, request.request_id)
        route = self._router.route(RouteRequest(request.tenant_id, request.product, request.model, request.region))
        reservation = self._gateway_billing.reserve(identity.tenant_id, request.product, request.model, request.request_id)
        result = self._pending(request)
        if result is None:
            self._audit.emit(audit_event("provider.execution", request.request_id, "started", tenant_id=identity.tenant_id, subject=identity.subject, product=request.product.value, model=request.model, provider=route.provider))
            try:
                result = self._providers.execute(
                    (route.provider,) + route.fallback,
                    ProviderRequest(request.request_id, identity.tenant_id, request.model, request.prompt),
                )
                if result.response.usage is None:
                    raise ProviderError("provider usage is missing", fallback_allowed=False, code="missing_usage", provider_response_id=result.response.provider_request_id)
                if not isinstance(result.response.usage, ProviderUsage):
                    raise ProviderError("provider usage is malformed", fallback_allowed=False, code="malformed_response", provider_response_id=result.response.provider_request_id)
                self._content_safety.authorize_output(identity.tenant_id, request.product, request.model, result.response.output_text, request.request_id)
            except Exception as original:
                self._compensate(reservation, request, original)
                raise
        self._remember_pending(request, result)
        usage = result.response.usage
        if usage is None or not isinstance(usage, ProviderUsage):  # Defensive guard for externally supplied results.
            code = "missing_usage" if usage is None else "malformed_response"
            original = ProviderError("provider usage is unavailable", fallback_allowed=False, code=code, provider_response_id=result.response.provider_request_id)
            self._compensate(reservation, request, original)
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
        self._clear_pending(request)
        self._audit.emit(audit_event("provider.execution", request.request_id, "succeeded", tenant_id=identity.tenant_id, subject=identity.subject, product=request.product.value, model=request.model, provider=result.provider_id))
        return SecureExecutionResult(
            request_id=request.request_id,
            provider=result.provider_id,
            model=request.model,
            region=request.region,
            output_text=result.response.output_text,
            provider_request_id=result.response.provider_request_id,
        )
