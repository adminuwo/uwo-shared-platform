"""Shared fail-closed boundaries used by Phase 3C services."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
from threading import RLock
from typing import Any, Callable, Mapping, Protocol

from packages.contracts import Permission, TenantStatus, VerifiedSubjectIdentity, contract_primitive, freeze_json, utc_now
from services.platform_control_plane.authorization import ControlPlaneAuthorizer, SubjectDirectory
from services.platform_control_plane.errors import AuthorizationDenied as CPAuthorizationDenied
from services.platform_control_plane.errors import ResourceNotFound as CPResourceNotFound
from services.platform_control_plane.repositories import TenantRepository


class DataServiceError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AuthorizationDenied(DataServiceError): pass
class ResourceNotFound(DataServiceError): pass
class Conflict(DataServiceError): pass
class InvalidRequest(DataServiceError): pass
class PolicyViolation(DataServiceError): pass
class InfrastructureUnavailable(DataServiceError): pass
class RepositoryIntegrityError(RuntimeError): pass


class DataServiceAuthorizer:
    """Composes every data service with the canonical Phase 3A boundary."""

    def __init__(self, tenants: TenantRepository, subjects: SubjectDirectory, control_plane: ControlPlaneAuthorizer, executors: frozenset[str] = frozenset()) -> None:
        self._tenants = tenants
        self._subjects = subjects
        self._control_plane = control_plane
        self._executors = executors

    @staticmethod
    def _translate(action: Callable[[], None]) -> None:
        try:
            action()
        except CPResourceNotFound as exc:
            raise ResourceNotFound(exc.code, "tenant does not exist") from exc
        except CPAuthorizationDenied as exc:
            raise AuthorizationDenied(exc.code, str(exc)) from exc

    def require(self, identity: VerifiedSubjectIdentity, tenant_id: str, permission: Permission, *, allow_suspended: bool = False) -> None:
        self._translate(lambda: self._control_plane.require(identity, tenant_id, permission, allow_suspended=allow_suspended))

    def require_platform_admin(self, identity: VerifiedSubjectIdentity) -> None:
        self._translate(lambda: self._control_plane.require_platform_admin(identity))

    def require_executor(self, identity: VerifiedSubjectIdentity, tenant_id: str, *, allow_suspended: bool = False) -> None:
        tenant = self._tenants.get(tenant_id)
        if tenant is None:
            raise ResourceNotFound("unknown_tenant", "tenant does not exist")
        if tenant.status is TenantStatus.SUSPENDED and not allow_suspended:
            raise AuthorizationDenied("tenant_suspended", "suspended tenants cannot perform new data mutations")
        if not self._subjects.exists(identity.subject):
            raise AuthorizationDenied("deprovisioned_subject", "executor is no longer active")
        if identity.subject not in self._executors:
            raise AuthorizationDenied("service_executor_required", "explicit service executor authorization is required")


@dataclass(frozen=True)
class ServiceAuditEvent:
    event_type: str
    request_id: str
    outcome: str
    tenant_id: str | None = None
    actor_subject: str | None = None
    reason_code: str | None = None
    resource_id: str | None = None
    occurred_at: str = ""

    def __post_init__(self) -> None:
        if not self.occurred_at: object.__setattr__(self, "occurred_at", utc_now())


class AuditSink(Protocol):
    def emit(self, event: ServiceAuditEvent) -> None: ...


class MemoryAuditSink:
    def __init__(self) -> None:
        self.events: list[ServiceAuditEvent] = []
        self._lock = RLock()

    def emit(self, event: ServiceAuditEvent) -> None:
        with self._lock: self.events.append(event)


class OutboxStatus(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    ACCEPTED = "accepted"
    CANCELLED = "cancelled"
    DEAD_LETTERED = "dead-lettered"


TERMINAL_OUTBOX_STATUSES = frozenset({OutboxStatus.ACCEPTED, OutboxStatus.CANCELLED, OutboxStatus.DEAD_LETTERED})
EVENT_ATTRIBUTE_ALLOWLIST = frozenset({
    "resource_id", "reason_code", "provider_id", "region", "product", "permission",
    "pseudonymous_subject_id", "status", "model", "channel", "outcome",
})
FORBIDDEN_EVENT_TERMS = frozenset({
    "prompt", "output", "body", "message", "token", "secret", "password", "credential",
    "authorization", "api_key", "storage_key", "download_token", "exception", "stack",
})


def _parse_utc(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an ISO-8601 UTC timestamp") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{name} must be an ISO-8601 UTC timestamp")
    return parsed


def _stable(value: str, name: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 256 or any(not (char.isalnum() or char in "._:@-") for char in value):
        raise ValueError(f"{name} must be a stable safe identifier")


def deterministic_id(namespace: str, *parts: object) -> str:
    payload = json.dumps([namespace, *(contract_primitive(part) for part in parts)], sort_keys=True, separators=(",", ":"), allow_nan=False)
    return f"{namespace}-{hashlib.sha256(payload.encode()).hexdigest()[:32]}"


def _event_id(event_type: str, tenant_id: str, request_id: str, attributes: Mapping[str, Any]) -> str:
    return deterministic_id("evt", event_type, tenant_id, request_id, attributes)


@dataclass(frozen=True)
class PlatformEvent:
    event_id: str
    event_type: str
    tenant_id: str
    occurred_at: str
    request_id: str
    attributes: Mapping[str, Any]
    schema_version: str = "1"

    def __post_init__(self) -> None:
        if self.schema_version != "1":
            raise ValueError("unsupported platform event schema version")
        for name, value in (("event_type", self.event_type), ("tenant_id", self.tenant_id), ("request_id", self.request_id)):
            _stable(value, name)
        _parse_utc(self.occurred_at, "occurred_at")
        if not isinstance(self.attributes, Mapping) or not set(self.attributes).issubset(EVENT_ATTRIBUTE_ALLOWLIST):
            raise ValueError("platform event attributes contain fields outside the allowlist")
        for key, value in self.attributes.items():
            lowered = key.lower()
            if any(term in lowered for term in FORBIDDEN_EVENT_TERMS):
                raise ValueError("platform event attribute name is sensitive")
            if value is not None and (isinstance(value, float) or not isinstance(value, (str, int, bool))):
                raise ValueError("platform event attributes must contain scalar JSON values")
            if isinstance(value, str) and len(value) > 256:
                raise ValueError("platform event attribute value is too long")
        frozen = freeze_json(self.attributes)
        object.__setattr__(self, "attributes", frozen)
        if self.event_id != _event_id(self.event_type, self.tenant_id, self.request_id, frozen):
            raise ValueError("event_id must contain the canonical platform event fingerprint")


@dataclass(frozen=True)
class OutboxRecord:
    record_id: str
    event: PlatformEvent
    status: OutboxStatus
    attempts: int
    next_attempt_at: str | None
    version: int
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    max_attempts: int = 5

    def __post_init__(self) -> None:
        _stable(self.record_id, "record_id")
        if not isinstance(self.event, PlatformEvent):
            raise ValueError("event must be a PlatformEvent")
        if not isinstance(self.status, OutboxStatus):
            raise ValueError("status must be an OutboxStatus")
        for name, value, minimum in (("attempts", self.attempts, 0), ("version", self.version, 1), ("max_attempts", self.max_attempts, 1)):
            if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
                raise ValueError(f"{name} must be an integer of at least {minimum}")
        if self.next_attempt_at is not None:
            _parse_utc(self.next_attempt_at, "next_attempt_at")
        if self.lease_expires_at is not None:
            _parse_utc(self.lease_expires_at, "lease_expires_at")
        if self.status is OutboxStatus.CLAIMED and (self.lease_owner is None or self.lease_expires_at is None):
            raise ValueError("claimed records require a lease owner and expiry")
        if self.status is not OutboxStatus.CLAIMED and (self.lease_owner is not None or self.lease_expires_at is not None):
            raise ValueError("only claimed records may carry lease metadata")


class EventPublisher(Protocol):
    def publish(self, event: PlatformEvent) -> None: ...


class EventRecorder(Protocol):
    def record(self, event: PlatformEvent) -> None: ...


class NullEventPublisher:
    def publish(self, event: PlatformEvent) -> None:
        del event


class NullEventRecorder:
    def record(self, event: PlatformEvent) -> None:
        del event


class CollectingEventPublisher:
    """Thread-safe idempotent test publisher."""

    def __init__(self) -> None:
        self._events: dict[str, PlatformEvent] = {}
        self._lock = RLock()

    def publish(self, event: PlatformEvent) -> None:
        with self._lock:
            existing = self._events.get(event.event_id)
            if existing is not None and existing != event:
                raise Conflict("event_id_conflict", "event ID already identifies different content")
            self._events[event.event_id] = event

    def record(self, event: PlatformEvent) -> None:
        self.publish(event)

    @property
    def events(self) -> tuple[PlatformEvent, ...]:
        with self._lock: return tuple(self._events.values())


class InMemoryOutbox:
    """Thread-safe test outbox with immutable claim/lease transitions."""

    def __init__(self) -> None:
        self._records: dict[str, OutboxRecord] = {}
        self._lock = RLock()

    def enqueue(self, record: OutboxRecord) -> OutboxRecord:
        with self._lock:
            existing = self._records.get(record.record_id)
            if existing is not None:
                if existing.event != record.event:
                    raise Conflict("outbox_conflict", "outbox ID has conflicting content")
                return existing
            self._records[record.record_id] = record
            return record

    def get(self, record_id: str) -> OutboxRecord | None:
        with self._lock:
            return self._records.get(record_id)

    def pending(self) -> tuple[OutboxRecord, ...]:
        with self._lock:
            return tuple(item for item in self._records.values() if item.status is OutboxStatus.PENDING)

    def eligible(self, now: str) -> tuple[OutboxRecord, ...]:
        instant = _parse_utc(now, "now")
        with self._lock:
            result = []
            for record in self._records.values():
                due = record.next_attempt_at is None or _parse_utc(record.next_attempt_at, "next_attempt_at") <= instant
                expired = record.status is OutboxStatus.CLAIMED and _parse_utc(record.lease_expires_at or "", "lease_expires_at") <= instant
                if (record.status is OutboxStatus.PENDING and due) or expired:
                    result.append(record)
            return tuple(sorted(result, key=lambda item: item.record_id))

    def claim(self, record_id: str, lease_owner: str, now: str, lease_seconds: int, expected_version: int | None = None) -> OutboxRecord:
        _stable(lease_owner, "lease_owner")
        instant = _parse_utc(now, "now")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds < 1:
            raise ValueError("lease_seconds must be a positive integer")
        with self._lock:
            current = self._records.get(record_id)
            if current is None:
                raise ResourceNotFound("unknown_outbox_record", "outbox record does not exist")
            if current.status in TERMINAL_OUTBOX_STATUSES:
                raise Conflict("outbox_terminal", "outbox record is terminal")
            if expected_version is not None and current.version != expected_version:
                raise Conflict("stale_version", "outbox version is stale")
            if current.status is OutboxStatus.CLAIMED and _parse_utc(current.lease_expires_at or "", "lease_expires_at") > instant:
                raise Conflict("outbox_already_claimed", "outbox record has an active lease")
            if current.status is OutboxStatus.PENDING and current.next_attempt_at is not None and _parse_utc(current.next_attempt_at, "next_attempt_at") > instant:
                raise Conflict("retry_not_due", "outbox retry is not due")
            if current.attempts >= current.max_attempts:
                raise Conflict("outbox_attempts_exhausted", "outbox owner must reconcile exhausted work")
            claimed = replace(
                current,
                status=OutboxStatus.CLAIMED,
                attempts=current.attempts + 1,
                next_attempt_at=None,
                lease_owner=lease_owner,
                lease_expires_at=(instant + timedelta(seconds=lease_seconds)).isoformat(),
                version=current.version + 1,
            )
            self._records[record_id] = claimed
            return claimed

    def acknowledge(self, record_id: str, lease_owner: str, expected_version: int, now: str | None = None) -> OutboxRecord:
        with self._lock:
            current = self._records.get(record_id)
            if current is None:
                raise ResourceNotFound("unknown_outbox_record", "outbox record does not exist")
            if current.status is OutboxStatus.ACCEPTED:
                return current
            self._require_claim(current, lease_owner, expected_version, now)
            accepted = replace(current, status=OutboxStatus.ACCEPTED, lease_owner=None, lease_expires_at=None, version=current.version + 1)
            self._records[record_id] = accepted
            return accepted

    def retry(self, record_id: str, lease_owner: str, expected_version: int, next_attempt_at: str, now: str | None = None) -> OutboxRecord:
        _parse_utc(next_attempt_at, "next_attempt_at")
        with self._lock:
            current = self._records.get(record_id)
            if current is None:
                raise ResourceNotFound("unknown_outbox_record", "outbox record does not exist")
            self._require_claim(current, lease_owner, expected_version, now)
            status = OutboxStatus.DEAD_LETTERED if current.attempts >= current.max_attempts else OutboxStatus.PENDING
            updated = replace(current, status=status, next_attempt_at=None if status is OutboxStatus.DEAD_LETTERED else next_attempt_at, lease_owner=None, lease_expires_at=None, version=current.version + 1)
            self._records[record_id] = updated
            return updated

    def cancel(self, record_id: str) -> OutboxRecord:
        with self._lock:
            current = self._records.get(record_id)
            if current is None:
                raise ResourceNotFound("unknown_outbox_record", "outbox record does not exist")
            if current.status in TERMINAL_OUTBOX_STATUSES:
                return current
            updated = replace(current, status=OutboxStatus.CANCELLED, next_attempt_at=None, lease_owner=None, lease_expires_at=None, version=current.version + 1)
            self._records[record_id] = updated
            return updated

    @staticmethod
    def _require_claim(current: OutboxRecord, lease_owner: str, expected_version: int, now: str | None = None) -> None:
        if current.status is not OutboxStatus.CLAIMED or current.lease_owner != lease_owner:
            raise Conflict("outbox_claim_required", "caller does not own the outbox lease")
        if current.version != expected_version:
            raise Conflict("stale_version", "outbox version is stale")
        if now is not None and _parse_utc(current.lease_expires_at or "", "lease_expires_at") <= _parse_utc(now, "now"):
            raise Conflict("outbox_lease_expired", "outbox lease has expired")

    def transition(self, record_id: str, status: OutboxStatus, expected_version: int, *, next_attempt_at: str | None = None, lease_owner: str | None = None, now: str | None = None) -> OutboxRecord:
        """Compatibility boundary for a claimed record's explicit terminal failure."""
        with self._lock:
            current = self._records.get(record_id)
            if current is None:
                raise ResourceNotFound("unknown_outbox_record", "outbox record does not exist")
            if current.status in TERMINAL_OUTBOX_STATUSES:
                if current.status is status:
                    return current
                raise Conflict("outbox_terminal", "outbox record is terminal")
            if current.version != expected_version:
                raise Conflict("stale_version", "outbox version is stale")
            if current.status is not OutboxStatus.CLAIMED or status is not OutboxStatus.DEAD_LETTERED or next_attempt_at is not None:
                raise Conflict("invalid_outbox_transition", "outbox transition is not permitted")
            if lease_owner is not None:
                self._require_claim(current, lease_owner, expected_version, now)
            updated = replace(current, status=status, next_attempt_at=next_attempt_at, lease_owner=None, lease_expires_at=None, version=current.version + 1)
            self._records[record_id] = updated
            return updated

    def reconcile_dead_letter(self, record_id: str, expected_version: int, now: str) -> OutboxRecord:
        """Let an owning service atomically reconcile exhausted business work."""
        instant = _parse_utc(now, "now")
        with self._lock:
            current = self._records.get(record_id)
            if current is None:
                raise ResourceNotFound("unknown_outbox_record", "outbox record does not exist")
            if current.status is OutboxStatus.DEAD_LETTERED:
                return current
            if current.status in TERMINAL_OUTBOX_STATUSES:
                raise Conflict("outbox_terminal", "outbox record is terminal")
            if current.version != expected_version:
                raise Conflict("stale_version", "outbox version is stale")
            if current.attempts < current.max_attempts:
                raise Conflict("outbox_attempts_available", "outbox attempts are not exhausted")
            if current.status is OutboxStatus.CLAIMED:
                expires = _parse_utc(current.lease_expires_at or "", "lease_expires_at")
                if expires > instant:
                    raise Conflict("outbox_already_claimed", "outbox record has an active lease")
            if current.status is OutboxStatus.PENDING and current.next_attempt_at is not None:
                if _parse_utc(current.next_attempt_at, "next_attempt_at") > instant:
                    raise Conflict("retry_not_due", "outbox retry is not due")
            updated = replace(
                current,
                status=OutboxStatus.DEAD_LETTERED,
                next_attempt_at=None,
                lease_owner=None,
                lease_expires_at=None,
                version=current.version + 1,
            )
            self._records[record_id] = updated
            return updated

    def snapshot(self) -> dict[str, OutboxRecord]:
        with self._lock:
            return dict(self._records)

    def restore(self, snapshot: dict[str, OutboxRecord]) -> None:
        with self._lock:
            self._records = dict(snapshot)


class OutboxEventRecorder:
    """Records events durably for a later publisher dispatcher."""

    def __init__(self, outbox: InMemoryOutbox, max_attempts: int = 5) -> None:
        self._outbox = outbox
        self._max_attempts = max_attempts

    def record(self, event: PlatformEvent) -> None:
        self._outbox.enqueue(OutboxRecord(deterministic_id("outbox", event.event_id), event, OutboxStatus.PENDING, 0, None, 1, max_attempts=self._max_attempts))


def record_event_safely(recorder: EventRecorder, event: PlatformEvent) -> bool:
    """Never let downstream event infrastructure change a completed operation result."""
    try:
        recorder.record(event)
    except Exception:
        return False
    return True


class OutboxDispatcher:
    """Claims before delivery and acknowledges only after idempotent acceptance."""

    def __init__(self, outbox: InMemoryOutbox, publisher: EventPublisher, owner: str, lease_seconds: int = 30) -> None:
        self._outbox = outbox
        self._publisher = publisher
        self._owner = owner
        self._lease_seconds = lease_seconds

    def dispatch(self, record_id: str, now: str, retry_at: str) -> OutboxRecord:
        claimed = self._outbox.claim(record_id, self._owner, now, self._lease_seconds)
        try:
            self._publisher.publish(claimed.event)
        except Exception:
            return self._outbox.retry(record_id, self._owner, claimed.version, retry_at, now)
        return self._outbox.acknowledge(record_id, self._owner, claimed.version, now)


def require_idempotency_key(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise InvalidRequest("invalid_idempotency_key", "idempotency key must contain 1 to 128 characters")


def platform_event(event_type: str, tenant_id: str, request_id: str, attributes: Mapping[str, Any], occurred_at: str | None = None) -> PlatformEvent:
    timestamp = occurred_at or utc_now()
    return PlatformEvent(_event_id(event_type, tenant_id, request_id, attributes), event_type, tenant_id, timestamp, request_id, attributes)
