"""Application service for billing accounts, credits, reservations, usage, and ledger."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import json
from typing import Callable, Type, TypeVar

from packages.contracts import (
    BillingAccount,
    BillingAccountStatus,
    BillingDecision,
    CreditReservation,
    LedgerEntry,
    LedgerEntryType,
    Product,
    ReservationStatus,
    UsageDimensions,
    UsageEvent,
    VerifiedSubjectIdentity,
    utc_now,
)
from services.platform_control_plane.repositories import TenantRepository

from .audit import AuditSink, audit_event
from .authorization import BillingAuthorizer
from .errors import Conflict, InvalidRequest, PaymentRequired, RepositoryIntegrityError, ResourceNotFound
from .pricing import calculate_charge
from .repositories import (
    AccountMutationResult,
    BillingAccountRepository,
    CaptureMutationResult,
    CreditMutationResult,
    IdempotencyRecord,
    IdempotencyResult,
    IdempotencyScope,
    LedgerPage,
    LedgerRepository,
    RateCardRepository,
    ReleaseMutationResult,
    ReservationMutationResult,
    ReservationRepository,
    UnitOfWorkFactory,
    UsageEventRepository,
)

T = TypeVar("T")


def _fingerprint(value: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _key(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise InvalidRequest("invalid_idempotency_key", "idempotency key must contain 1 to 128 characters")


def _positive(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise InvalidRequest("invalid_amount", f"{name} must be a positive integer microunit amount")


def _id(prefix: str, value: str) -> str:
    return f"{prefix}:{hashlib.sha256(value.encode()).hexdigest()[:32]}"


def _replay(record: IdempotencyRecord, fingerprint: str, expected_type: Type[T]) -> T:
    if record.request_fingerprint != fingerprint:
        raise Conflict("idempotency_conflict", "idempotency key was already used with different request input")
    if not isinstance(record.original_result, expected_type):
        raise RepositoryIntegrityError("idempotency result type does not match operation")
    return record.original_result


class PlatformBillingService:
    def __init__(
        self,
        tenants: TenantRepository,
        accounts: BillingAccountRepository,
        ledger: LedgerRepository,
        reservations: ReservationRepository,
        usage: UsageEventRepository,
        rate_cards: RateCardRepository,
        unit_of_work: UnitOfWorkFactory,
        authorizer: BillingAuthorizer,
        audit: AuditSink,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self._tenants = tenants
        self._accounts = accounts
        self._ledger = ledger
        self._reservations = reservations
        self._usage = usage
        self._rate_cards = rate_cards
        self._unit_of_work = unit_of_work
        self._authorizer = authorizer
        self._audit = audit
        self._clock = clock

    @staticmethod
    def _scope(operation: str, tenant_id: str, identity: VerifiedSubjectIdentity) -> IdempotencyScope:
        return IdempotencyScope(operation, tenant_id, identity.subject)

    def _account(self, tenant_id: str) -> BillingAccount:
        account = self._accounts.get_by_tenant(tenant_id)
        if account is None:
            raise ResourceNotFound("unknown_billing_account", "billing account does not exist")
        return account

    def _active_account(self, tenant_id: str) -> BillingAccount:
        account = self._account(tenant_id)
        if account.status is BillingAccountStatus.CLOSED:
            raise Conflict("billing_account_closed", "closed billing accounts cannot accept debit activity")
        if account.status is BillingAccountStatus.SUSPENDED:
            raise Conflict("billing_account_suspended", "suspended billing accounts cannot accept debit activity")
        return account

    def _rollback(self, request_id: str, identity: VerifiedSubjectIdentity, tenant_id: str) -> None:
        self._audit.emit(audit_event("billing.transaction_rolled_back", request_id, "failed", actor_subject=identity.subject, tenant_id=tenant_id, reason_code="transaction_rolled_back"))

    def _early_replay(self, scope: IdempotencyScope, key: str, fingerprint: str, expected_type: Type[T]) -> T | None:
        """Replay before consulting mutable lifecycle state."""
        with self._unit_of_work() as transaction:
            record = transaction.idempotency.get(scope, key)
            if record is None:
                transaction.commit()
                return None
            result = _replay(record, fingerprint, expected_type)
            transaction.commit()
            return result

    def create_account(self, identity: VerifiedSubjectIdentity, tenant_id: str, idempotency_key: str, request_id: str, expected_version: int = 0) -> AccountMutationResult:
        self._authorizer.require_platform_admin(identity)
        _key(idempotency_key)
        if expected_version != 0:
            raise Conflict("stale_version", "new billing account expected_version must be zero")
        if self._tenants.get(tenant_id) is None:
            raise ResourceNotFound("unknown_tenant", "tenant does not exist")
        timestamp = self._clock()
        account = BillingAccount(f"billing:{tenant_id}", tenant_id, BillingAccountStatus.ACTIVE, timestamp, timestamp, 1)
        scope = self._scope("billing.account.create", tenant_id, identity)
        fingerprint = _fingerprint({"tenant_id": tenant_id, "expected_version": expected_version})
        mutation_started = False
        try:
            with self._unit_of_work() as transaction:
                record = transaction.idempotency.get(scope, idempotency_key)
                if record is not None:
                    result = AccountMutationResult(_replay(record, fingerprint, BillingAccount), False)
                    transaction.commit()
                    return result
                mutation_started = True
                transaction.accounts.create(account)
                transaction.idempotency.put(IdempotencyRecord(scope, idempotency_key, fingerprint, account))
                transaction.commit()
        except RepositoryIntegrityError:
            if mutation_started:
                self._rollback(request_id, identity, tenant_id)
            raise
        self._audit.emit(audit_event("billing.account_created", request_id, "succeeded", actor_subject=identity.subject, tenant_id=tenant_id, account_id=account.account_id))
        return AccountMutationResult(account, True)

    def read_account(self, identity: VerifiedSubjectIdentity, tenant_id: str, request_id: str) -> BillingAccount:
        self._authorizer.require_read(identity, tenant_id)
        return self._account(tenant_id)

    def read_balance(self, identity: VerifiedSubjectIdentity, tenant_id: str, request_id: str):
        self._authorizer.require_read(identity, tenant_id)
        account = self._account(tenant_id)
        return self._ledger.balance(account.account_id, tenant_id, self._clock())

    def set_account_status(self, identity: VerifiedSubjectIdentity, tenant_id: str, status: BillingAccountStatus, expected_version: int, request_id: str) -> BillingAccount:
        self._authorizer.require_platform_admin(identity)
        current = self._account(tenant_id)
        if current.status is BillingAccountStatus.CLOSED:
            raise Conflict("billing_account_closed", "closed billing accounts cannot be reopened")
        if current.status is status:
            raise Conflict("status_unchanged", "billing account already has the requested status")
        updated = replace(current, status=status, updated_at=self._clock(), version=current.version + 1)
        stored = self._accounts.update(updated, expected_version)
        self._audit.emit(audit_event("billing.account_status_changed", request_id, "succeeded", actor_subject=identity.subject, tenant_id=tenant_id, account_id=stored.account_id))
        return stored

    def _credit_mutation(
        self,
        identity: VerifiedSubjectIdentity,
        tenant_id: str,
        delta: int,
        expected_version: int,
        idempotency_key: str,
        request_id: str,
        operation: str,
        entry_type: LedgerEntryType,
    ) -> CreditMutationResult:
        self._authorizer.require_platform_admin(identity)
        _key(idempotency_key)
        if not isinstance(delta, int) or isinstance(delta, bool) or delta == 0:
            raise InvalidRequest("invalid_amount", "credit mutation must be a non-zero integer microunit amount")
        scope = self._scope(operation, tenant_id, identity)
        fingerprint = _fingerprint({"tenant_id": tenant_id, "delta": delta, "expected_version": expected_version})
        replay = self._early_replay(scope, idempotency_key, fingerprint, CreditMutationResult)
        if replay is not None:
            return replace(replay, created=False)
        account = self._account(tenant_id)
        if account.status is BillingAccountStatus.CLOSED and delta < 0:
            raise Conflict("billing_account_closed", "closed billing accounts cannot accept debit activity")
        timestamp = self._clock()
        entry = LedgerEntry(
            _id("ledger", f"{operation}:{tenant_id}:{identity.subject}:{idempotency_key}"), account.account_id, tenant_id,
            entry_type, abs(delta), delta, 0, _id("credit", idempotency_key), timestamp, identity.subject, expected_version + 1,
        )
        mutation_started = False
        try:
            with self._unit_of_work() as transaction:
                record = transaction.idempotency.get(scope, idempotency_key)
                if record is not None:
                    replay = _replay(record, fingerprint, CreditMutationResult)
                    transaction.commit()
                    return replace(replay, created=False)
                mutation_started = True
                balance = transaction.ledger.append(entry, expected_version)
                result = CreditMutationResult(balance, entry, True)
                transaction.idempotency.put(IdempotencyRecord(scope, idempotency_key, fingerprint, result))
                transaction.commit()
        except RepositoryIntegrityError:
            if mutation_started:
                self._rollback(request_id, identity, tenant_id)
            raise
        self._audit.emit(audit_event(f"billing.{entry_type.value}", request_id, "succeeded", actor_subject=identity.subject, tenant_id=tenant_id, account_id=account.account_id, ledger_entry_id=entry.entry_id))
        return result

    def grant_credits(self, identity, tenant_id, amount_microunits, expected_version, idempotency_key, request_id):
        _positive(amount_microunits, "amount_microunits")
        return self._credit_mutation(identity, tenant_id, amount_microunits, expected_version, idempotency_key, request_id, "billing.credit.grant", LedgerEntryType.CREDIT_GRANT)

    def adjust_credits(self, identity, tenant_id, delta_microunits, expected_version, idempotency_key, request_id):
        return self._credit_mutation(identity, tenant_id, delta_microunits, expected_version, idempotency_key, request_id, "billing.credit.adjust", LedgerEntryType.CREDIT_ADJUSTMENT)

    def refund(self, identity, tenant_id, amount_microunits, expected_version, idempotency_key, request_id):
        _positive(amount_microunits, "amount_microunits")
        return self._credit_mutation(identity, tenant_id, amount_microunits, expected_version, idempotency_key, request_id, "billing.credit.refund", LedgerEntryType.REFUND)

    def authorize_estimated_charge(self, identity: VerifiedSubjectIdentity, tenant_id: str, amount_microunits: int, request_id: str) -> BillingDecision:
        self._authorizer.require_executor(identity, tenant_id)
        _positive(amount_microunits, "estimated_microunits")
        account = self._active_account(tenant_id)
        balance = self._ledger.balance(account.account_id, tenant_id, self._clock())
        if balance.available_microunits < amount_microunits:
            self._audit.emit(audit_event("billing.insufficient_balance", request_id, "denied", actor_subject=identity.subject, tenant_id=tenant_id, account_id=account.account_id, reason_code="insufficient_balance"))
            raise PaymentRequired("insufficient_balance", "available credit balance is insufficient")
        return BillingDecision(_id("decision", request_id), tenant_id, True, amount_microunits, "authorized", self._clock())

    def reserve(
        self,
        identity: VerifiedSubjectIdentity,
        tenant_id: str,
        product: Product,
        model: str,
        request_id: str,
        amount_microunits: int,
        expires_at: str,
        expected_balance_version: int,
        idempotency_key: str,
    ) -> ReservationMutationResult:
        self._authorizer.require_executor(identity, tenant_id)
        _positive(amount_microunits, "estimated_microunits")
        _key(idempotency_key)
        scope = self._scope("billing.reservation.create", tenant_id, identity)
        fingerprint = _fingerprint({"tenant_id": tenant_id, "product": product.value, "model": model, "request_id": request_id, "amount": amount_microunits, "expires_at": expires_at, "expected_balance_version": expected_balance_version})
        replay = self._early_replay(scope, idempotency_key, fingerprint, ReservationMutationResult)
        if replay is not None:
            return replace(replay, created=False)
        account = self._active_account(tenant_id)
        timestamp = self._clock()
        try:
            if datetime.fromisoformat(expires_at.replace("Z", "+00:00")) <= datetime.fromisoformat(timestamp.replace("Z", "+00:00")):
                raise InvalidRequest("invalid_expiry", "reservation expiry must be in the future")
        except (TypeError, ValueError) as exc:
            if isinstance(exc, InvalidRequest):
                raise
            raise InvalidRequest("invalid_expiry", "reservation expiry must be an ISO-8601 UTC timestamp") from exc
        reservation_id = _id("reservation", f"{tenant_id}:{request_id}")
        reservation = CreditReservation(reservation_id, account.account_id, tenant_id, product, model, request_id, amount_microunits, 0, 0, ReservationStatus.ACTIVE, timestamp, expires_at, timestamp, 1)
        entry = LedgerEntry(_id("ledger", f"reserve:{reservation_id}"), account.account_id, tenant_id, LedgerEntryType.USAGE_RESERVATION, amount_microunits, -amount_microunits, amount_microunits, reservation_id, timestamp, identity.subject, expected_balance_version + 1)
        mutation_started = False
        try:
            with self._unit_of_work() as transaction:
                record = transaction.idempotency.get(scope, idempotency_key)
                if record is not None:
                    replay = _replay(record, fingerprint, ReservationMutationResult)
                    transaction.commit()
                    return replace(replay, created=False)
                mutation_started = True
                transaction.reservations.create(reservation)
                balance = transaction.ledger.append(entry, expected_balance_version)
                result = ReservationMutationResult(reservation, balance, entry, True)
                transaction.idempotency.put(IdempotencyRecord(scope, idempotency_key, fingerprint, result))
                transaction.commit()
        except RepositoryIntegrityError:
            if mutation_started:
                self._rollback(request_id, identity, tenant_id)
            raise
        except Conflict as exc:
            if exc.code == "insufficient_balance":
                self._audit.emit(audit_event("billing.insufficient_balance", request_id, "denied", actor_subject=identity.subject, tenant_id=tenant_id, account_id=account.account_id, reason_code="insufficient_balance"))
                raise PaymentRequired("insufficient_balance", "available credit balance is insufficient") from exc
            raise
        self._audit.emit(audit_event("billing.reservation_created", request_id, "succeeded", actor_subject=identity.subject, tenant_id=tenant_id, account_id=account.account_id, reservation_id=reservation_id, ledger_entry_id=entry.entry_id))
        return result

    def capture(
        self,
        identity: VerifiedSubjectIdentity,
        reservation_id: str,
        usage_event_id: str,
        provider_id: str,
        provider_model_id: str | None,
        region: str,
        provider_request_id: str | None,
        dimensions: UsageDimensions,
        expected_reservation_version: int,
        expected_balance_version: int,
        idempotency_key: str,
        request_id: str,
        administrative_override: bool = False,
    ) -> CaptureMutationResult:
        reservation = self._reservations.get(reservation_id)
        if reservation is None:
            raise ResourceNotFound("unknown_reservation", "credit reservation does not exist")
        if administrative_override:
            self._authorizer.require_platform_admin(identity)
            self._authorizer.require_active_tenant(reservation.tenant_id)
        else:
            self._authorizer.require_executor(identity, reservation.tenant_id)
        _key(idempotency_key)
        scope = self._scope("billing.reservation.capture", reservation.tenant_id, identity)
        fingerprint = _fingerprint({"reservation_id": reservation_id, "usage_event_id": usage_event_id, "provider_id": provider_id, "provider_model_id": provider_model_id, "region": region, "provider_request_id": provider_request_id, "input": dimensions.input_tokens, "output": dimensions.output_tokens, "total": dimensions.total_tokens, "expected_reservation_version": expected_reservation_version, "expected_balance_version": expected_balance_version, "administrative_override": administrative_override})
        replay = self._early_replay(scope, idempotency_key, fingerprint, CaptureMutationResult)
        if replay is not None:
            return replace(replay, created=False)
        account = self._active_account(reservation.tenant_id)
        if reservation.status not in (ReservationStatus.PENDING, ReservationStatus.ACTIVE, ReservationStatus.PARTIALLY_CAPTURED):
            raise Conflict("invalid_reservation_transition", "reservation cannot be captured from its current state")
        timestamp = self._clock()
        if datetime.fromisoformat(reservation.expires_at.replace("Z", "+00:00")) <= datetime.fromisoformat(timestamp.replace("Z", "+00:00")) and not administrative_override:
            raise Conflict("reservation_expired", "expired reservations require an audited platform-administrator override")
        rate_card = self._rate_cards.active_at(timestamp)
        charge = calculate_charge(rate_card, reservation.product, reservation.model, provider_id, region, dimensions, timestamp)
        _positive(charge.total_charge_microunits, "calculated_charge_microunits")
        remaining = reservation.estimated_microunits - reservation.captured_microunits - reservation.released_microunits
        if charge.total_charge_microunits > remaining:
            raise Conflict("capture_exceeds_reservation", "captured amount cannot exceed remaining reservation")
        captured = reservation.captured_microunits + charge.total_charge_microunits
        status = ReservationStatus.CAPTURED if captured == reservation.estimated_microunits else ReservationStatus.PARTIALLY_CAPTURED
        updated = replace(reservation, captured_microunits=captured, status=status, updated_at=timestamp, version=reservation.version + 1)
        usage = UsageEvent(usage_event_id, reservation_id, reservation.tenant_id, reservation.product, reservation.model, provider_id, provider_model_id, region, reservation.request_id, provider_request_id, dimensions, timestamp, rate_card.rate_card_id, rate_card.version, charge.total_charge_microunits)
        entry = LedgerEntry(_id("ledger", f"capture:{usage_event_id}"), account.account_id, reservation.tenant_id, LedgerEntryType.USAGE_CAPTURE, charge.total_charge_microunits, 0, -charge.total_charge_microunits, reservation_id, timestamp, identity.subject, expected_balance_version + 1)
        mutation_started = False
        try:
            with self._unit_of_work() as transaction:
                record = transaction.idempotency.get(scope, idempotency_key)
                if record is not None:
                    replay = _replay(record, fingerprint, CaptureMutationResult)
                    transaction.commit()
                    return replace(replay, created=False)
                mutation_started = True
                transaction.reservations.update(updated, expected_reservation_version)
                transaction.usage.append(usage)
                balance = transaction.ledger.append(entry, expected_balance_version)
                result = CaptureMutationResult(updated, balance, usage, charge, entry, True)
                transaction.idempotency.put(IdempotencyRecord(scope, idempotency_key, fingerprint, result))
                transaction.commit()
        except RepositoryIntegrityError:
            if mutation_started:
                self._rollback(request_id, identity, reservation.tenant_id)
            raise
        event_type = "billing.reservation_captured_override" if administrative_override else "billing.reservation_captured"
        self._audit.emit(audit_event(event_type, request_id, "succeeded", actor_subject=identity.subject, tenant_id=reservation.tenant_id, account_id=account.account_id, reservation_id=reservation_id, usage_event_id=usage_event_id, ledger_entry_id=entry.entry_id))
        return result

    def release(self, identity: VerifiedSubjectIdentity, reservation_id: str, expected_reservation_version: int, expected_balance_version: int, idempotency_key: str, request_id: str) -> ReleaseMutationResult:
        reservation = self._reservations.get(reservation_id)
        if reservation is None:
            raise ResourceNotFound("unknown_reservation", "credit reservation does not exist")
        self._authorizer.require_executor(identity, reservation.tenant_id, allow_suspended=True)
        _key(idempotency_key)
        scope = self._scope("billing.reservation.release", reservation.tenant_id, identity)
        fingerprint = _fingerprint({"reservation_id": reservation_id, "expected_reservation_version": expected_reservation_version, "expected_balance_version": expected_balance_version})
        replay = self._early_replay(scope, idempotency_key, fingerprint, ReleaseMutationResult)
        if replay is not None:
            return replace(replay, created=False)
        if reservation.status not in (ReservationStatus.PENDING, ReservationStatus.ACTIVE, ReservationStatus.PARTIALLY_CAPTURED):
            raise Conflict("invalid_reservation_transition", "reservation cannot be released from its current state")
        remaining = reservation.estimated_microunits - reservation.captured_microunits - reservation.released_microunits
        _positive(remaining, "remaining_microunits")
        account = self._account(reservation.tenant_id)
        timestamp = self._clock()
        updated = replace(reservation, released_microunits=reservation.released_microunits + remaining, status=ReservationStatus.RELEASED, updated_at=timestamp, version=reservation.version + 1)
        entry = LedgerEntry(_id("ledger", f"release:{reservation_id}:{expected_reservation_version}"), account.account_id, reservation.tenant_id, LedgerEntryType.RESERVATION_RELEASE, remaining, remaining, -remaining, reservation_id, timestamp, identity.subject, expected_balance_version + 1)
        mutation_started = False
        try:
            with self._unit_of_work() as transaction:
                record = transaction.idempotency.get(scope, idempotency_key)
                if record is not None:
                    replay = _replay(record, fingerprint, ReleaseMutationResult)
                    transaction.commit()
                    return replace(replay, created=False)
                mutation_started = True
                transaction.reservations.update(updated, expected_reservation_version)
                balance = transaction.ledger.append(entry, expected_balance_version)
                result = ReleaseMutationResult(updated, balance, entry, True)
                transaction.idempotency.put(IdempotencyRecord(scope, idempotency_key, fingerprint, result))
                transaction.commit()
        except RepositoryIntegrityError:
            if mutation_started:
                self._rollback(request_id, identity, reservation.tenant_id)
            raise
        self._audit.emit(audit_event("billing.reservation_released", request_id, "succeeded", actor_subject=identity.subject, tenant_id=reservation.tenant_id, account_id=account.account_id, reservation_id=reservation_id, ledger_entry_id=entry.entry_id))
        return result

    def read_usage(self, identity: VerifiedSubjectIdentity, tenant_id: str, usage_event_id: str, request_id: str) -> UsageEvent:
        self._authorizer.require_read(identity, tenant_id)
        event = self._usage.get(usage_event_id)
        if event is None or event.tenant_id != tenant_id:
            raise ResourceNotFound("unknown_usage_event", "usage transaction does not exist")
        return event

    def list_ledger(self, identity: VerifiedSubjectIdentity, tenant_id: str, limit: int, cursor: str | None, request_id: str) -> LedgerPage:
        self._authorizer.require_read(identity, tenant_id)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 100:
            raise InvalidRequest("invalid_pagination", "limit must be between 1 and 100")
        account = self._account(tenant_id)
        return self._ledger.list(account.account_id, limit, cursor)

    def active_rate_card(self, identity: VerifiedSubjectIdentity, tenant_id: str, request_id: str):
        self._authorizer.require_read(identity, tenant_id)
        return self._rate_cards.active_at(self._clock()).identity

    def reserve_for_gateway(self, identity, tenant_id, product, model, request_id, amount_microunits, reservation_seconds, idempotency_key):
        """Atomically reserve against the current balance; callers never carry versions."""

        self._authorizer.require_executor(identity, tenant_id)
        _positive(amount_microunits, "estimated_microunits")
        _positive(reservation_seconds, "reservation_seconds")
        _key(idempotency_key)
        scope = self._scope("billing.gateway.reserve", tenant_id, identity)
        fingerprint = _fingerprint({"tenant_id": tenant_id, "product": product.value, "model": model, "request_id": request_id, "amount": amount_microunits, "reservation_seconds": reservation_seconds})
        mutation_started = False
        try:
            with self._unit_of_work() as transaction:
                record = transaction.idempotency.get(scope, idempotency_key)
                if record is not None:
                    replay = _replay(record, fingerprint, ReservationMutationResult)
                    transaction.commit()
                    return replace(replay, created=False)
                account = transaction.accounts.get_by_tenant(tenant_id)
                if account is None:
                    raise ResourceNotFound("unknown_billing_account", "billing account does not exist")
                if account.status is BillingAccountStatus.CLOSED:
                    raise Conflict("billing_account_closed", "closed billing accounts cannot accept debit activity")
                if account.status is BillingAccountStatus.SUSPENDED:
                    raise Conflict("billing_account_suspended", "suspended billing accounts cannot accept debit activity")
                timestamp = self._clock()
                expires_at = (datetime.fromisoformat(timestamp.replace("Z", "+00:00")) + timedelta(seconds=reservation_seconds)).isoformat()
                balance_before = transaction.ledger.balance(account.account_id, tenant_id, timestamp)
                reservation_id = _id("reservation", f"{tenant_id}:{request_id}")
                reservation = CreditReservation(reservation_id, account.account_id, tenant_id, product, model, request_id, amount_microunits, 0, 0, ReservationStatus.ACTIVE, timestamp, expires_at, timestamp, 1)
                entry = LedgerEntry(_id("ledger", f"reserve:{reservation_id}"), account.account_id, tenant_id, LedgerEntryType.USAGE_RESERVATION, amount_microunits, -amount_microunits, amount_microunits, reservation_id, timestamp, identity.subject, balance_before.version + 1)
                mutation_started = True
                transaction.reservations.create(reservation)
                balance = transaction.ledger.append(entry, balance_before.version)
                result = ReservationMutationResult(reservation, balance, entry, True)
                transaction.idempotency.put(IdempotencyRecord(scope, idempotency_key, fingerprint, result))
                transaction.commit()
        except RepositoryIntegrityError:
            if mutation_started:
                self._rollback(request_id, identity, tenant_id)
            raise
        except Conflict as exc:
            if exc.code == "insufficient_balance":
                self._audit.emit(audit_event("billing.insufficient_balance", request_id, "denied", actor_subject=identity.subject, tenant_id=tenant_id, reason_code="insufficient_balance"))
                raise PaymentRequired("insufficient_balance", "available credit balance is insufficient") from exc
            raise
        self._audit.emit(audit_event("billing.reservation_created", request_id, "succeeded", actor_subject=identity.subject, tenant_id=tenant_id, account_id=account.account_id, reservation_id=reservation.reservation_id, ledger_entry_id=entry.entry_id))
        return result

    def capture_for_gateway(self, identity, tenant_id, reservation_id, usage_event_id, provider_id, provider_model_id, region, provider_request_id, dimensions, idempotency_key, request_id):
        """Atomically load current lifecycle state, capture usage, and release unused credit."""

        _key(idempotency_key)
        self._authorizer.require_executor(identity, tenant_id)
        scope = self._scope("billing.gateway.capture", tenant_id, identity)
        fingerprint = _fingerprint({"reservation_id": reservation_id, "usage_event_id": usage_event_id, "provider_id": provider_id, "provider_model_id": provider_model_id, "region": region, "provider_request_id": provider_request_id, "input": dimensions.input_tokens, "output": dimensions.output_tokens, "total": dimensions.total_tokens})
        mutation_started = False
        try:
            with self._unit_of_work() as transaction:
                record = transaction.idempotency.get(scope, idempotency_key)
                if record is not None:
                    replay = _replay(record, fingerprint, CaptureMutationResult)
                    transaction.commit()
                    return replace(replay, created=False)
                reservation = transaction.reservations.get(reservation_id)
                if reservation is None or reservation.tenant_id != tenant_id:
                    raise ResourceNotFound("unknown_reservation", "credit reservation does not exist")
                account = transaction.accounts.get_by_tenant(reservation.tenant_id)
                if account is None:
                    raise ResourceNotFound("unknown_billing_account", "billing account does not exist")
                if account.status is not BillingAccountStatus.ACTIVE:
                    raise Conflict(f"billing_account_{account.status.value}", "billing account cannot accept debit activity")
                if reservation.status not in (ReservationStatus.PENDING, ReservationStatus.ACTIVE, ReservationStatus.PARTIALLY_CAPTURED):
                    raise Conflict("invalid_reservation_transition", "reservation cannot be captured from its current state")
                timestamp = self._clock()
                if datetime.fromisoformat(reservation.expires_at.replace("Z", "+00:00")) <= datetime.fromisoformat(timestamp.replace("Z", "+00:00")):
                    raise Conflict("reservation_expired", "expired reservations require an audited platform-administrator override")
                rate_card = transaction.rate_cards.active_at(timestamp)
                charge = calculate_charge(rate_card, reservation.product, reservation.model, provider_id, region, dimensions, timestamp)
                _positive(charge.total_charge_microunits, "calculated_charge_microunits")
                remaining = reservation.estimated_microunits - reservation.captured_microunits - reservation.released_microunits
                if charge.total_charge_microunits > remaining:
                    raise Conflict("capture_exceeds_reservation", "captured amount cannot exceed remaining reservation")
                balance_before = transaction.ledger.balance(account.account_id, reservation.tenant_id, timestamp)
                captured_total = reservation.captured_microunits + charge.total_charge_microunits
                unused = remaining - charge.total_charge_microunits
                final_status = ReservationStatus.CAPTURED if unused == 0 else ReservationStatus.RELEASED
                updated = replace(reservation, captured_microunits=captured_total, released_microunits=reservation.released_microunits + unused, status=final_status, updated_at=timestamp, version=reservation.version + 1)
                usage = UsageEvent(usage_event_id, reservation_id, reservation.tenant_id, reservation.product, reservation.model, provider_id, provider_model_id, region, reservation.request_id, provider_request_id, dimensions, timestamp, rate_card.rate_card_id, rate_card.version, charge.total_charge_microunits)
                capture_entry = LedgerEntry(_id("ledger", f"capture:{usage_event_id}"), account.account_id, reservation.tenant_id, LedgerEntryType.USAGE_CAPTURE, charge.total_charge_microunits, 0, -charge.total_charge_microunits, reservation_id, timestamp, identity.subject, balance_before.version + 1)
                mutation_started = True
                transaction.reservations.update(updated, reservation.version)
                transaction.usage.append(usage)
                balance = transaction.ledger.append(capture_entry, balance_before.version)
                if unused:
                    release_entry = LedgerEntry(_id("ledger", f"release-after-capture:{usage_event_id}"), account.account_id, reservation.tenant_id, LedgerEntryType.RESERVATION_RELEASE, unused, unused, -unused, reservation_id, timestamp, identity.subject, balance.version + 1)
                    balance = transaction.ledger.append(release_entry, balance.version)
                result = CaptureMutationResult(updated, balance, usage, charge, capture_entry, True)
                transaction.idempotency.put(IdempotencyRecord(scope, idempotency_key, fingerprint, result))
                transaction.commit()
        except RepositoryIntegrityError:
            if mutation_started:
                self._rollback(request_id, identity, tenant_id)
            raise
        self._audit.emit(audit_event("billing.reservation_captured", request_id, "succeeded", actor_subject=identity.subject, tenant_id=tenant_id, account_id=account.account_id, reservation_id=reservation_id, usage_event_id=usage_event_id, ledger_entry_id=capture_entry.entry_id))
        return result

    def release_for_gateway(self, identity, tenant_id, reservation_id, idempotency_key, request_id):
        """Atomically compensate using current reservation and ledger state."""

        _key(idempotency_key)
        self._authorizer.require_executor(identity, tenant_id, allow_suspended=True)
        scope = self._scope("billing.gateway.release", tenant_id, identity)
        fingerprint = _fingerprint({"reservation_id": reservation_id})
        mutation_started = False
        try:
            with self._unit_of_work() as transaction:
                record = transaction.idempotency.get(scope, idempotency_key)
                if record is not None:
                    replay = _replay(record, fingerprint, ReleaseMutationResult)
                    transaction.commit()
                    return replace(replay, created=False)
                reservation = transaction.reservations.get(reservation_id)
                if reservation is None or reservation.tenant_id != tenant_id:
                    raise ResourceNotFound("unknown_reservation", "credit reservation does not exist")
                if reservation.status not in (ReservationStatus.PENDING, ReservationStatus.ACTIVE, ReservationStatus.PARTIALLY_CAPTURED):
                    raise Conflict("invalid_reservation_transition", "reservation cannot be released from its current state")
                account = transaction.accounts.get_by_tenant(reservation.tenant_id)
                if account is None:
                    raise ResourceNotFound("unknown_billing_account", "billing account does not exist")
                timestamp = self._clock()
                balance_before = transaction.ledger.balance(account.account_id, reservation.tenant_id, timestamp)
                remaining = reservation.estimated_microunits - reservation.captured_microunits - reservation.released_microunits
                _positive(remaining, "remaining_microunits")
                updated = replace(reservation, released_microunits=reservation.released_microunits + remaining, status=ReservationStatus.RELEASED, updated_at=timestamp, version=reservation.version + 1)
                entry = LedgerEntry(_id("ledger", f"gateway-release:{reservation_id}"), account.account_id, reservation.tenant_id, LedgerEntryType.RESERVATION_RELEASE, remaining, remaining, -remaining, reservation_id, timestamp, identity.subject, balance_before.version + 1)
                mutation_started = True
                transaction.reservations.update(updated, reservation.version)
                balance = transaction.ledger.append(entry, balance_before.version)
                result = ReleaseMutationResult(updated, balance, entry, True)
                transaction.idempotency.put(IdempotencyRecord(scope, idempotency_key, fingerprint, result))
                transaction.commit()
        except RepositoryIntegrityError:
            if mutation_started:
                self._rollback(request_id, identity, tenant_id)
            raise
        self._audit.emit(audit_event("billing.reservation_released", request_id, "succeeded", actor_subject=identity.subject, tenant_id=tenant_id, account_id=account.account_id, reservation_id=reservation_id, ledger_entry_id=entry.entry_id))
        return result
