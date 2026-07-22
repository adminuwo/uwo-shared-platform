"""Shared fail-closed boundaries used by Phase 3C services."""

from __future__ import annotations

from dataclasses import dataclass, replace
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
    ACCEPTED = "accepted"
    DEAD_LETTERED = "dead-lettered"


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
        object.__setattr__(self, "attributes", freeze_json(self.attributes))


@dataclass(frozen=True)
class OutboxRecord:
    record_id: str
    event: PlatformEvent
    status: OutboxStatus
    attempts: int
    next_attempt_at: str | None
    version: int


class EventPublisher(Protocol):
    def publish(self, event: PlatformEvent) -> None: ...


class NullEventPublisher:
    def publish(self, event: PlatformEvent) -> None:
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

    @property
    def events(self) -> tuple[PlatformEvent, ...]:
        with self._lock: return tuple(self._events.values())


class InMemoryOutbox:
    """Test-only immutable-record outbox with version-controlled transitions."""

    def __init__(self) -> None:
        self._records: dict[str, OutboxRecord] = {}
        self._lock = RLock()

    def enqueue(self, record: OutboxRecord) -> OutboxRecord:
        existing = self._records.get(record.record_id)
        if existing is not None:
            if existing.event != record.event: raise Conflict("outbox_conflict", "outbox ID has conflicting content")
            return existing
        self._records[record.record_id] = record
        return record

    def get(self, record_id: str) -> OutboxRecord | None: return self._records.get(record_id)
    def pending(self) -> tuple[OutboxRecord, ...]: return tuple(item for item in self._records.values() if item.status is OutboxStatus.PENDING)

    def transition(self, record_id: str, status: OutboxStatus, expected_version: int, *, next_attempt_at: str | None = None) -> OutboxRecord:
        current = self._records.get(record_id)
        if current is None: raise ResourceNotFound("unknown_outbox_record", "outbox record does not exist")
        if current.version != expected_version: raise Conflict("stale_version", "outbox version is stale")
        updated = replace(current, status=status, attempts=current.attempts + 1, next_attempt_at=next_attempt_at, version=current.version + 1)
        self._records[record_id] = updated
        return updated

    def snapshot(self) -> dict[str, OutboxRecord]: return dict(self._records)
    def restore(self, snapshot: dict[str, OutboxRecord]) -> None: self._records = dict(snapshot)


def deterministic_id(namespace: str, *parts: object) -> str:
    payload = json.dumps([namespace, *(contract_primitive(part) for part in parts)], sort_keys=True, separators=(",", ":"), allow_nan=False)
    return f"{namespace}-{hashlib.sha256(payload.encode()).hexdigest()[:32]}"


def require_idempotency_key(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise InvalidRequest("invalid_idempotency_key", "idempotency key must contain 1 to 128 characters")


def platform_event(event_type: str, tenant_id: str, request_id: str, attributes: Mapping[str, Any], occurred_at: str | None = None) -> PlatformEvent:
    timestamp = occurred_at or utc_now()
    return PlatformEvent(deterministic_id("evt", event_type, tenant_id, request_id), event_type, tenant_id, timestamp, request_id, attributes)
