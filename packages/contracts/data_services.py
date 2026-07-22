"""Canonical contracts for platform data, notification, analytics, and audit services."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Any, Mapping

from .catalog import Product
from .domain import IDENTIFIER, SCHEMA_VERSION, freeze_json, thaw_json


def _utc(value: str, name: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an ISO-8601 UTC timestamp") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{name} must be an ISO-8601 UTC timestamp")


def _identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must be a stable safe identifier")


def _positive(value: int, name: str, *, allow_zero: bool = False) -> None:
    minimum = 0 if allow_zero else 1
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")


def _base(schema_version: str, identifiers: Mapping[str, str], timestamps: Mapping[str, str], version: int | None = None) -> None:
    if schema_version != SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {SCHEMA_VERSION!r}")
    for name, value in identifiers.items():
        _identifier(value, name)
    for name, value in timestamps.items():
        _utc(value, name)
    if version is not None:
        _positive(version, "version")


def _context(tenant_id: str, product: Product, region: str) -> None:
    _identifier(tenant_id, "tenant_id")
    _identifier(region, "region")
    if not isinstance(product, Product):
        raise ValueError("product must be a Product")


def _immutable_json(value: Mapping[str, Any], *, allowed: frozenset[str] | None = None) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("metadata must be a JSON object")
    if allowed is not None and not set(value).issubset(allowed):
        raise ValueError("metadata contains fields outside the allowlist")
    frozen = freeze_json(value)
    if not isinstance(frozen, MappingProxyType):
        raise ValueError("metadata must be a JSON object")
    return frozen


def contract_primitive(value: Any) -> Any:
    """Convert a contract to deterministic JSON primitives."""
    if is_dataclass(value):
        return {field.name: contract_primitive(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {key: contract_primitive(item) for key, item in sorted(value.items())}
    if isinstance(value, tuple):
        return [contract_primitive(item) for item in value]
    return value


def contract_json(value: Any) -> str:
    return json.dumps(contract_primitive(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def contract_fingerprint(value: Any) -> str:
    return hashlib.sha256(contract_json(value).encode()).hexdigest()


class ObjectClassification(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"
    REGULATED = "regulated"


class ObjectStatus(str, Enum):
    ACTIVE = "active"
    DELETED = "deleted"


class UploadStatus(str, Enum):
    INITIATED = "initiated"
    FINALIZED = "finalized"
    ABORTED = "aborted"


class MalwareScanStatus(str, Enum):
    PENDING = "pending"
    CLEAN = "clean"
    INFECTED = "infected"
    ERROR = "error"


@dataclass(frozen=True)
class ContentIntegrityMetadata:
    algorithm: str
    digest: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION or self.algorithm not in {"sha256", "sha384", "sha512"}:
            raise ValueError("content integrity requires a supported SHA-2 algorithm")
        lengths = {"sha256": 64, "sha384": 96, "sha512": 128}
        if not isinstance(self.digest, str) or len(self.digest) != lengths[self.algorithm] or any(c not in "0123456789abcdef" for c in self.digest):
            raise ValueError("digest must be lowercase hexadecimal matching the algorithm")


@dataclass(frozen=True)
class StoredObject:
    object_id: str
    tenant_id: str
    product: Product
    region: str
    classification: ObjectClassification
    status: ObjectStatus
    current_version: int
    created_at: str
    updated_at: str
    version: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"object_id": self.object_id}, {"created_at": self.created_at, "updated_at": self.updated_at}, self.version)
        _context(self.tenant_id, self.product, self.region)
        _positive(self.current_version, "current_version")
        if not isinstance(self.classification, ObjectClassification) or not isinstance(self.status, ObjectStatus):
            raise ValueError("classification and status must use canonical enums")


@dataclass(frozen=True)
class ObjectVersion:
    object_version_id: str
    object_id: str
    tenant_id: str
    product: Product
    region: str
    version_number: int
    storage_key: str
    content_length: int
    integrity: ContentIntegrityMetadata
    malware_scan_status: MalwareScanStatus
    created_at: str
    created_by: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"object_version_id": self.object_version_id, "object_id": self.object_id, "storage_key": self.storage_key, "created_by": self.created_by}, {"created_at": self.created_at})
        _context(self.tenant_id, self.product, self.region)
        _positive(self.version_number, "version_number")
        _positive(self.content_length, "content_length", allow_zero=True)
        if not isinstance(self.integrity, ContentIntegrityMetadata) or not isinstance(self.malware_scan_status, MalwareScanStatus):
            raise ValueError("integrity and malware_scan_status must use canonical contracts")


@dataclass(frozen=True)
class UploadSession:
    upload_id: str
    tenant_id: str
    product: Product
    region: str
    object_id: str
    classification: ObjectClassification
    storage_key: str
    expected_content_length: int
    expected_integrity: ContentIntegrityMetadata
    status: UploadStatus
    created_at: str
    expires_at: str
    version: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"upload_id": self.upload_id, "object_id": self.object_id, "storage_key": self.storage_key}, {"created_at": self.created_at, "expires_at": self.expires_at}, self.version)
        _context(self.tenant_id, self.product, self.region)
        _positive(self.expected_content_length, "expected_content_length", allow_zero=True)
        if not isinstance(self.classification, ObjectClassification) or not isinstance(self.status, UploadStatus):
            raise ValueError("classification and status must use canonical enums")


@dataclass(frozen=True)
class RetentionPolicy:
    policy_id: str
    tenant_id: str
    object_id: str
    retain_until: str
    created_at: str
    version: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"policy_id": self.policy_id, "tenant_id": self.tenant_id, "object_id": self.object_id}, {"retain_until": self.retain_until, "created_at": self.created_at}, self.version)


@dataclass(frozen=True)
class LegalHold:
    hold_id: str
    tenant_id: str
    object_id: str
    active: bool
    reason_code: str
    created_at: str
    updated_at: str
    version: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"hold_id": self.hold_id, "tenant_id": self.tenant_id, "object_id": self.object_id, "reason_code": self.reason_code}, {"created_at": self.created_at, "updated_at": self.updated_at}, self.version)
        if not isinstance(self.active, bool):
            raise ValueError("active must be boolean")


@dataclass(frozen=True)
class DownloadAuthorization:
    authorization_id: str
    tenant_id: str
    object_id: str
    object_version_id: str
    opaque_token: str
    expires_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"authorization_id": self.authorization_id, "tenant_id": self.tenant_id, "object_id": self.object_id, "object_version_id": self.object_version_id, "opaque_token": self.opaque_token}, {"expires_at": self.expires_at})


class NotificationChannel(str, Enum):
    EMAIL = "email"
    SMS = "sms"
    WHATSAPP = "whatsapp"
    PUSH = "push"
    IN_APP = "in-app"
    WEBHOOK = "webhook"


class NotificationStatus(str, Enum):
    PENDING = "pending"
    ENQUEUED = "enqueued"
    DELIVERED = "delivered"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPPRESSED = "suppressed"
    DEAD_LETTERED = "dead-lettered"


class DeliveryOutcome(str, Enum):
    ACCEPTED = "accepted"
    RETRYABLE_FAILURE = "retryable-failure"
    PERMANENT_FAILURE = "permanent-failure"


@dataclass(frozen=True)
class NotificationTemplate:
    template_id: str
    tenant_id: str
    product: Product
    region: str
    active_version: int | None
    created_at: str
    version: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"template_id": self.template_id}, {"created_at": self.created_at}, self.version)
        _context(self.tenant_id, self.product, self.region)
        if self.active_version is not None:
            _positive(self.active_version, "active_version")


@dataclass(frozen=True)
class TemplateVersion:
    template_version_id: str
    template_id: str
    tenant_id: str
    version_number: int
    channel: NotificationChannel
    content_reference: str
    variable_keys: tuple[str, ...]
    created_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"template_version_id": self.template_version_id, "template_id": self.template_id, "tenant_id": self.tenant_id, "content_reference": self.content_reference}, {"created_at": self.created_at})
        _positive(self.version_number, "version_number")
        if tuple(sorted(set(self.variable_keys))) != self.variable_keys:
            raise ValueError("variable_keys must be unique and sorted")
        for key in self.variable_keys:
            _identifier(key, "variable_key")


@dataclass(frozen=True)
class Notification:
    notification_id: str
    tenant_id: str
    product: Product
    region: str
    template_id: str
    template_version: int
    channel: NotificationChannel
    recipient_reference: str
    deduplication_key: str
    status: NotificationStatus
    created_at: str
    updated_at: str
    version: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"notification_id": self.notification_id, "template_id": self.template_id, "recipient_reference": self.recipient_reference, "deduplication_key": self.deduplication_key}, {"created_at": self.created_at, "updated_at": self.updated_at}, self.version)
        _context(self.tenant_id, self.product, self.region)
        _positive(self.template_version, "template_version")


@dataclass(frozen=True)
class DeliveryAttempt:
    attempt_id: str
    notification_id: str
    tenant_id: str
    attempt_number: int
    outcome: DeliveryOutcome
    provider_reference: str | None
    reason_code: str | None
    attempted_at: str
    next_attempt_at: str | None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        identifiers = {"attempt_id": self.attempt_id, "notification_id": self.notification_id, "tenant_id": self.tenant_id}
        if self.provider_reference is not None: identifiers["provider_reference"] = self.provider_reference
        if self.reason_code is not None: identifiers["reason_code"] = self.reason_code
        timestamps = {"attempted_at": self.attempted_at}
        if self.next_attempt_at is not None: timestamps["next_attempt_at"] = self.next_attempt_at
        _base(self.schema_version, identifiers, timestamps)
        _positive(self.attempt_number, "attempt_number")


@dataclass(frozen=True)
class NotificationPreference:
    preference_id: str
    tenant_id: str
    subject_reference: str
    channel: NotificationChannel
    enabled: bool
    updated_at: str
    version: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"preference_id": self.preference_id, "tenant_id": self.tenant_id, "subject_reference": self.subject_reference}, {"updated_at": self.updated_at}, self.version)
        if not isinstance(self.enabled, bool): raise ValueError("enabled must be boolean")


@dataclass(frozen=True)
class DeadLetterRecord:
    dead_letter_id: str
    notification_id: str
    tenant_id: str
    reason_code: str
    final_attempt: int
    created_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"dead_letter_id": self.dead_letter_id, "notification_id": self.notification_id, "tenant_id": self.tenant_id, "reason_code": self.reason_code}, {"created_at": self.created_at})
        _positive(self.final_attempt, "final_attempt")


class AnalyticsEventType(str, Enum):
    REQUEST_COMPLETED = "request.completed"
    REQUEST_FAILED = "request.failed"
    PROVIDER_FAILED = "provider.failed"
    TENANT_STATUS_CHANGED = "tenant.status-changed"
    BILLING_BALANCE_LOW = "billing.balance-low"
    STORAGE_SCAN_COMPLETED = "storage.scan-completed"
    NOTIFICATION_DELIVERY = "notification.delivery"
    AUDIT_VERIFICATION = "audit.verification"


@dataclass(frozen=True)
class AnalyticsDimensions:
    outcome: str
    duration_bucket: str | None = None
    token_count_bucket: str | None = None
    credit_usage_bucket: str | None = None
    error_code: str | None = None
    pseudonymous_subject_id: str | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        values = {"outcome": self.outcome, "duration_bucket": self.duration_bucket, "token_count_bucket": self.token_count_bucket, "credit_usage_bucket": self.credit_usage_bucket, "error_code": self.error_code, "pseudonymous_subject_id": self.pseudonymous_subject_id}
        for name, value in values.items():
            if value is not None: _identifier(value, name)
        if self.schema_version != SCHEMA_VERSION: raise ValueError("invalid schema_version")


@dataclass(frozen=True)
class AnalyticsEvent:
    event_id: str
    tenant_id: str
    product: Product
    region: str
    event_type: AnalyticsEventType
    dimensions: AnalyticsDimensions
    occurred_at: str
    recorded_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"event_id": self.event_id}, {"occurred_at": self.occurred_at, "recorded_at": self.recorded_at})
        _context(self.tenant_id, self.product, self.region)
        if not isinstance(self.event_type, AnalyticsEventType) or not isinstance(self.dimensions, AnalyticsDimensions): raise ValueError("event type and dimensions must use canonical contracts")


@dataclass(frozen=True)
class AggregationWindow:
    start_at: str
    end_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {}, {"start_at": self.start_at, "end_at": self.end_at})
        if self.start_at >= self.end_at: raise ValueError("aggregation window start must precede end")


@dataclass(frozen=True)
class MetricDefinition:
    metric_id: str
    event_type: AnalyticsEventType
    minimum_export_count: int
    version: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"metric_id": self.metric_id}, {}, self.version)
        _positive(self.minimum_export_count, "minimum_export_count")


@dataclass(frozen=True)
class MetricPoint:
    metric_id: str
    tenant_id: str
    product: Product
    region: str
    event_type: AnalyticsEventType
    window: AggregationWindow
    count: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"metric_id": self.metric_id}, {})
        _context(self.tenant_id, self.product, self.region)
        _positive(self.count, "count", allow_zero=True)


@dataclass(frozen=True)
class AnalyticsSnapshot:
    snapshot_id: str
    tenant_id: str
    window: AggregationWindow
    points: tuple[MetricPoint, ...]
    created_at: str
    integrity_hash: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"snapshot_id": self.snapshot_id, "tenant_id": self.tenant_id, "integrity_hash": self.integrity_hash}, {"created_at": self.created_at})


AUDIT_ATTRIBUTE_ALLOWLIST = frozenset({"resource_id", "reason_code", "provider_id", "region", "product", "permission", "pseudonymous_subject_id", "status"})


@dataclass(frozen=True)
class DurableAuditEvent:
    event_id: str
    tenant_id: str
    sequence: int
    action: str
    outcome: str
    occurred_at: str
    request_id: str
    actor_subject: str | None
    attributes: Mapping[str, Any]
    previous_hash: str
    current_hash: str
    redacted: bool = True
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        ids = {"event_id": self.event_id, "tenant_id": self.tenant_id, "action": self.action, "outcome": self.outcome, "request_id": self.request_id}
        if self.actor_subject is not None: ids["actor_subject"] = self.actor_subject
        _base(self.schema_version, ids, {"occurred_at": self.occurred_at})
        _positive(self.sequence, "sequence")
        if not isinstance(self.redacted, bool): raise ValueError("redacted must be boolean")
        if any(value is not None and (isinstance(value, float) or not isinstance(value, (str, int, bool))) for value in self.attributes.values()):
            raise ValueError("audit attributes must contain only allowlisted scalar values")
        object.__setattr__(self, "attributes", _immutable_json(self.attributes, allowed=AUDIT_ATTRIBUTE_ALLOWLIST))
        for value in (self.previous_hash, self.current_hash):
            if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value): raise ValueError("audit hashes must be SHA-256 hex")


@dataclass(frozen=True)
class AuditSequence:
    tenant_id: str
    next_sequence: int
    version: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"tenant_id": self.tenant_id}, {}, self.version); _positive(self.next_sequence, "next_sequence")


@dataclass(frozen=True)
class AuditCheckpoint:
    checkpoint_id: str
    tenant_id: str
    through_sequence: int
    event_hash: str
    created_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"checkpoint_id": self.checkpoint_id, "tenant_id": self.tenant_id, "event_hash": self.event_hash}, {"created_at": self.created_at}); _positive(self.through_sequence, "through_sequence")


@dataclass(frozen=True)
class AuditIntegrityProof:
    tenant_id: str
    valid: bool
    checked_events: int
    first_invalid_sequence: int | None
    verified_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"tenant_id": self.tenant_id}, {"verified_at": self.verified_at}); _positive(self.checked_events, "checked_events", allow_zero=True)


@dataclass(frozen=True)
class AuditExportManifest:
    export_id: str
    tenant_id: str
    first_sequence: int
    last_sequence: int
    event_count: int
    integrity_hash: str
    created_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"export_id": self.export_id, "tenant_id": self.tenant_id, "integrity_hash": self.integrity_hash}, {"created_at": self.created_at}); _positive(self.event_count, "event_count", allow_zero=True)


@dataclass(frozen=True)
class AuditRetentionPolicy:
    policy_id: str
    tenant_id: str
    retain_until: str
    legal_hold: bool
    created_at: str
    version: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"policy_id": self.policy_id, "tenant_id": self.tenant_id}, {"retain_until": self.retain_until, "created_at": self.created_at}, self.version)
        if not isinstance(self.legal_hold, bool): raise ValueError("legal_hold must be boolean")
