"""Canonical Phase 3D tenant-governance and operational reliability contracts."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Any, Mapping

from .domain import IDENTIFIER, SCHEMA_VERSION, freeze_json


def _identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must be a stable safe identifier")


def _text(value: str, name: str, maximum: int = 1024) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must contain 1 to {maximum} characters")


def _utc(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an ISO-8601 UTC timestamp") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{name} must be an ISO-8601 UTC timestamp")
    return parsed


def _integer(value: int, name: str, minimum: int = 0) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")


def _base(
    schema_version: str,
    identifiers: Mapping[str, str],
    timestamps: Mapping[str, str] = MappingProxyType({}),
    version: int | None = None,
) -> None:
    if schema_version != SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {SCHEMA_VERSION!r}")
    for name, value in identifiers.items():
        _identifier(value, name)
    for name, value in timestamps.items():
        _utc(value, name)
    if version is not None:
        _integer(version, "version", 1)


def _sorted_identifiers(values: tuple[str, ...], name: str) -> None:
    if tuple(sorted(set(values))) != values:
        raise ValueError(f"{name} must be unique and deterministically sorted")
    for value in values:
        _identifier(value, name)


def _unique_identifiers(values: tuple[str, ...], name: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{name} values must be unique")
    for value in values:
        _identifier(value, name)


def _immutable_metadata(
    value: Mapping[str, Any],
    *,
    allowed: frozenset[str] | None = None,
    forbidden_terms: frozenset[str] = frozenset(),
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("metadata must be a JSON object")
    if allowed is not None and not set(value).issubset(allowed):
        raise ValueError("metadata contains fields outside the allowlist")
    def inspect(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                lowered = key.lower()
                if any(term in lowered for term in forbidden_terms):
                    raise ValueError("metadata contains a forbidden sensitive or executable field")
                inspect(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                inspect(nested)
        elif forbidden_terms and isinstance(item, str):
            lowered = item.lower()
            if "://" in lowered or any(marker in lowered for marker in ("#!", "subprocess", "import os", "curl ", "wget ", "-----begin")):
                raise ValueError("metadata contains executable, credential, or endpoint content")
    inspect(value)
    if allowed is not None and any(item is not None and (isinstance(item, float) or not isinstance(item, (str, int, bool))) for item in value.values()):
        raise ValueError("allowlisted operational metadata values must be scalar")
    frozen = freeze_json(value)
    if not isinstance(frozen, MappingProxyType):
        raise ValueError("metadata must be a JSON object")
    return frozen


def operations_primitive(value: Any) -> Any:
    if is_dataclass(value):
        return {item.name: operations_primitive(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {key: operations_primitive(item) for key, item in sorted(value.items())}
    if isinstance(value, tuple):
        return [operations_primitive(item) for item in value]
    return value


def operations_json(value: Any) -> str:
    return json.dumps(operations_primitive(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def operations_fingerprint(value: Any) -> str:
    return hashlib.sha256(operations_json(value).encode()).hexdigest()


class TenantAdministrationWorkflowStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    BLOCKED = "blocked"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TenantAdministrationStepStatus(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class TenantAdministrationWorkflow:
    workflow_id: str
    tenant_id: str
    workflow_type: str
    status: TenantAdministrationWorkflowStatus
    step_ids: tuple[str, ...]
    current_step: int
    requested_by: str
    created_at: str
    updated_at: str
    version: int
    cancellation_requested: bool = False
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"workflow_id": self.workflow_id, "tenant_id": self.tenant_id, "workflow_type": self.workflow_type, "requested_by": self.requested_by}, {"created_at": self.created_at, "updated_at": self.updated_at}, self.version)
        _unique_identifiers(self.step_ids, "step_id")
        _integer(self.current_step, "current_step")
        if self.current_step > len(self.step_ids):
            raise ValueError("current_step exceeds the workflow step count")
        if not isinstance(self.status, TenantAdministrationWorkflowStatus) or not isinstance(self.cancellation_requested, bool):
            raise ValueError("workflow status and cancellation flag must use canonical types")


@dataclass(frozen=True)
class TenantAdministrationStep:
    step_id: str
    workflow_id: str
    tenant_id: str
    operation: str
    status: TenantAdministrationStepStatus
    idempotency_key: str
    attempt_count: int
    created_at: str
    updated_at: str
    version: int
    claimed_by: str | None = None
    claim_expires_at: str | None = None
    error_code: str | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        identifiers = {"step_id": self.step_id, "workflow_id": self.workflow_id, "tenant_id": self.tenant_id, "operation": self.operation, "idempotency_key": self.idempotency_key}
        if self.claimed_by is not None:
            identifiers["claimed_by"] = self.claimed_by
        if self.error_code is not None:
            identifiers["error_code"] = self.error_code
        timestamps = {"created_at": self.created_at, "updated_at": self.updated_at}
        if self.claim_expires_at is not None:
            timestamps["claim_expires_at"] = self.claim_expires_at
        _base(self.schema_version, identifiers, timestamps, self.version)
        _integer(self.attempt_count, "attempt_count")
        if not isinstance(self.status, TenantAdministrationStepStatus):
            raise ValueError("status must be a TenantAdministrationStepStatus")
        if self.status is TenantAdministrationStepStatus.CLAIMED and (self.claimed_by is None or self.claim_expires_at is None):
            raise ValueError("claimed steps require owner and lease expiry")


@dataclass(frozen=True)
class WorkflowReceipt:
    receipt_id: str
    workflow_id: str
    step_id: str
    tenant_id: str
    operation: str
    result_reference: str
    result_digest: str
    completed_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"receipt_id": self.receipt_id, "workflow_id": self.workflow_id, "step_id": self.step_id, "tenant_id": self.tenant_id, "operation": self.operation, "result_reference": self.result_reference}, {"completed_at": self.completed_at})
        if not isinstance(self.result_digest, str) or len(self.result_digest) != 64 or any(char not in "0123456789abcdef" for char in self.result_digest):
            raise ValueError("result_digest must be a lowercase SHA-256 digest")


@dataclass(frozen=True)
class TenantOperationalProfile:
    tenant_id: str
    region: str
    tenant_status: str
    billing_status: str
    active_policy_release_id: str
    storage_writes_allowed: bool
    notifications_allowed: bool
    observed_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"tenant_id": self.tenant_id, "region": self.region, "tenant_status": self.tenant_status, "billing_status": self.billing_status, "active_policy_release_id": self.active_policy_release_id}, {"observed_at": self.observed_at})
        if not isinstance(self.storage_writes_allowed, bool) or not isinstance(self.notifications_allowed, bool):
            raise ValueError("operational profile flags must be boolean")


@dataclass(frozen=True)
class _TenantPlan:
    plan_id: str
    tenant_id: str
    region: str
    requested_by: str
    step_operations: tuple[str, ...]
    created_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"plan_id": self.plan_id, "tenant_id": self.tenant_id, "region": self.region, "requested_by": self.requested_by}, {"created_at": self.created_at})
        _unique_identifiers(self.step_operations, "step_operation")


@dataclass(frozen=True)
class TenantOnboardingPlan(_TenantPlan):
    pass


@dataclass(frozen=True)
class TenantSuspensionPlan(_TenantPlan):
    pass


@dataclass(frozen=True)
class TenantReactivationPlan(_TenantPlan):
    pass


@dataclass(frozen=True)
class TenantDecommissionPlan(_TenantPlan):
    evidence_preservation_required: bool = True

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.evidence_preservation_required is not True:
            raise ValueError("decommission plans must preserve durable evidence")


class PolicyDraftStatus(str, Enum):
    DRAFT = "draft"
    VALIDATED = "validated"
    SUBMITTED = "submitted"
    REJECTED = "rejected"
    RELEASED = "released"


class ApprovalDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class PolicyEnvironment(str, Enum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


POLICY_FORBIDDEN_TERMS = frozenset({"secret", "password", "credential", "token", "api_key", "endpoint", "url", "script", "command", "executable", "code"})
HIGH_RISK_CATEGORIES = frozenset({"regional-policy", "model-entitlement", "retention-reduction", "audit-policy", "billing-enforcement", "provider-allowlist"})


@dataclass(frozen=True)
class PolicyDraft:
    draft_id: str
    tenant_id: str
    proposer_subject: str
    status: PolicyDraftStatus
    compatibility_version: str
    content: Mapping[str, Any]
    base_release_id: str | None
    risk_categories: tuple[str, ...]
    created_at: str
    updated_at: str
    version: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        identifiers = {"draft_id": self.draft_id, "tenant_id": self.tenant_id, "proposer_subject": self.proposer_subject, "compatibility_version": self.compatibility_version}
        if self.base_release_id is not None:
            identifiers["base_release_id"] = self.base_release_id
        _base(self.schema_version, identifiers, {"created_at": self.created_at, "updated_at": self.updated_at}, self.version)
        if not isinstance(self.status, PolicyDraftStatus):
            raise ValueError("status must be a PolicyDraftStatus")
        object.__setattr__(self, "content", _immutable_metadata(self.content, forbidden_terms=POLICY_FORBIDDEN_TERMS))
        _sorted_identifiers(self.risk_categories, "risk_category")
        if not set(self.risk_categories).issubset(HIGH_RISK_CATEGORIES):
            raise ValueError("risk_categories contain an unknown high-risk category")


@dataclass(frozen=True)
class PolicyValidationResult:
    validation_id: str
    draft_id: str
    tenant_id: str
    valid: bool
    compatibility_version: str
    error_codes: tuple[str, ...]
    validated_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"validation_id": self.validation_id, "draft_id": self.draft_id, "tenant_id": self.tenant_id, "compatibility_version": self.compatibility_version}, {"validated_at": self.validated_at})
        if not isinstance(self.valid, bool):
            raise ValueError("valid must be boolean")
        _sorted_identifiers(self.error_codes, "error_code")
        if self.valid and self.error_codes:
            raise ValueError("valid policy validation cannot contain errors")


@dataclass(frozen=True)
class PolicyChangeRequest:
    change_request_id: str
    draft_id: str
    tenant_id: str
    proposer_subject: str
    validation_id: str
    status: PolicyDraftStatus
    required_approvals: int
    created_at: str
    version: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"change_request_id": self.change_request_id, "draft_id": self.draft_id, "tenant_id": self.tenant_id, "proposer_subject": self.proposer_subject, "validation_id": self.validation_id}, {"created_at": self.created_at}, self.version)
        _integer(self.required_approvals, "required_approvals", 1)
        if self.status not in {PolicyDraftStatus.SUBMITTED, PolicyDraftStatus.REJECTED, PolicyDraftStatus.RELEASED}:
            raise ValueError("change-request status is invalid")


@dataclass(frozen=True)
class PolicyApproval:
    approval_id: str
    change_request_id: str
    tenant_id: str
    approver_subject: str
    decision: ApprovalDecision
    reason_code: str
    decided_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"approval_id": self.approval_id, "change_request_id": self.change_request_id, "tenant_id": self.tenant_id, "approver_subject": self.approver_subject, "reason_code": self.reason_code}, {"decided_at": self.decided_at})
        if not isinstance(self.decision, ApprovalDecision):
            raise ValueError("decision must be an ApprovalDecision")


@dataclass(frozen=True)
class ConfigurationDigest:
    algorithm: str
    digest: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION or self.algorithm != "sha256":
            raise ValueError("configuration digest must use schema 1 and SHA-256")
        if len(self.digest) != 64 or any(char not in "0123456789abcdef" for char in self.digest):
            raise ValueError("digest must be lowercase SHA-256 hexadecimal")


@dataclass(frozen=True)
class PolicyRelease:
    release_id: str
    tenant_id: str
    change_request_id: str
    compatibility_version: str
    content: Mapping[str, Any]
    digest: ConfigurationDigest
    source_release_id: str | None
    created_by: str
    created_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        identifiers = {"release_id": self.release_id, "tenant_id": self.tenant_id, "change_request_id": self.change_request_id, "compatibility_version": self.compatibility_version, "created_by": self.created_by}
        if self.source_release_id is not None:
            identifiers["source_release_id"] = self.source_release_id
        _base(self.schema_version, identifiers, {"created_at": self.created_at})
        object.__setattr__(self, "content", _immutable_metadata(self.content, forbidden_terms=POLICY_FORBIDDEN_TERMS))
        if self.digest.digest != hashlib.sha256(operations_json(self.content).encode()).hexdigest():
            raise ValueError("policy release digest does not match canonical content")


@dataclass(frozen=True)
class PolicyPromotion:
    promotion_id: str
    tenant_id: str
    release_id: str
    environment: PolicyEnvironment
    promoted_by: str
    promoted_at: str
    environment_version: int
    previous_release_id: str | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        identifiers = {"promotion_id": self.promotion_id, "tenant_id": self.tenant_id, "release_id": self.release_id, "promoted_by": self.promoted_by}
        if self.previous_release_id is not None:
            identifiers["previous_release_id"] = self.previous_release_id
        _base(self.schema_version, identifiers, {"promoted_at": self.promoted_at}, self.environment_version)
        if not isinstance(self.environment, PolicyEnvironment):
            raise ValueError("environment must be a PolicyEnvironment")


@dataclass(frozen=True)
class PolicyRollback:
    rollback_id: str
    tenant_id: str
    environment: PolicyEnvironment
    from_release_id: str
    target_release_id: str
    new_promotion_id: str
    requested_by: str
    created_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"rollback_id": self.rollback_id, "tenant_id": self.tenant_id, "from_release_id": self.from_release_id, "target_release_id": self.target_release_id, "new_promotion_id": self.new_promotion_id, "requested_by": self.requested_by}, {"created_at": self.created_at})
        if not isinstance(self.environment, PolicyEnvironment):
            raise ValueError("environment must be a PolicyEnvironment")


@dataclass(frozen=True)
class ConfigurationBundle:
    bundle_id: str
    tenant_id: str
    environment: PolicyEnvironment
    release_ids: tuple[str, ...]
    digest: ConfigurationDigest
    created_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"bundle_id": self.bundle_id, "tenant_id": self.tenant_id}, {"created_at": self.created_at})
        _sorted_identifiers(self.release_ids, "release_id")
        if not isinstance(self.environment, PolicyEnvironment):
            raise ValueError("environment must be a PolicyEnvironment")


class ServiceHealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class MetricKind(str, Enum):
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"
    DURATION = "duration"


TELEMETRY_METADATA_ALLOWLIST = frozenset({"component_id", "tenant_id", "region", "environment", "operation", "outcome", "error_code"})


@dataclass(frozen=True)
class ServiceIdentity:
    service_id: str
    component_id: str
    environment: PolicyEnvironment
    region: str
    registered_at: str
    version: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"service_id": self.service_id, "component_id": self.component_id, "region": self.region}, {"registered_at": self.registered_at}, self.version)
        if not isinstance(self.environment, PolicyEnvironment):
            raise ValueError("environment must be a PolicyEnvironment")


@dataclass(frozen=True)
class HistogramBucket:
    upper_bound: int
    count: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported histogram schema")
        _integer(self.upper_bound, "upper_bound")
        _integer(self.count, "count")


@dataclass(frozen=True)
class OperationalMetric:
    metric_id: str
    service_id: str
    name: str
    kind: MetricKind
    unit: str
    monotonic: bool
    histogram_bounds: tuple[int, ...]
    created_at: str
    version: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"metric_id": self.metric_id, "service_id": self.service_id, "name": self.name, "unit": self.unit}, {"created_at": self.created_at}, self.version)
        if not isinstance(self.kind, MetricKind) or not isinstance(self.monotonic, bool):
            raise ValueError("metric kind and monotonic flag must use canonical types")
        if tuple(sorted(set(self.histogram_bounds))) != self.histogram_bounds or any(not isinstance(value, int) or value < 0 for value in self.histogram_bounds):
            raise ValueError("histogram bounds must be unique sorted non-negative integers")
        if self.kind is not MetricKind.HISTOGRAM and self.histogram_bounds:
            raise ValueError("only histogram metrics may define buckets")


@dataclass(frozen=True)
class MetricSample:
    sample_id: str
    metric_id: str
    service_id: str
    observed_at: str
    value: int
    buckets: tuple[HistogramBucket, ...]
    metadata: Mapping[str, Any]
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"sample_id": self.sample_id, "metric_id": self.metric_id, "service_id": self.service_id}, {"observed_at": self.observed_at})
        _integer(self.value, "value")
        object.__setattr__(self, "metadata", _immutable_metadata(self.metadata, allowed=TELEMETRY_METADATA_ALLOWLIST))
        bounds = tuple(item.upper_bound for item in self.buckets)
        if tuple(sorted(set(bounds))) != bounds:
            raise ValueError("histogram buckets must have unique increasing bounds")


@dataclass(frozen=True)
class TelemetryBatch:
    batch_id: str
    service_id: str
    sample_ids: tuple[str, ...]
    received_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"batch_id": self.batch_id, "service_id": self.service_id}, {"received_at": self.received_at})
        _sorted_identifiers(self.sample_ids, "sample_id")


@dataclass(frozen=True)
class TelemetryCheckpoint:
    checkpoint_id: str
    service_id: str
    through_sample_id: str
    sample_count: int
    digest: ConfigurationDigest
    created_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"checkpoint_id": self.checkpoint_id, "service_id": self.service_id, "through_sample_id": self.through_sample_id}, {"created_at": self.created_at})
        _integer(self.sample_count, "sample_count")


@dataclass(frozen=True)
class DependencyHealth:
    dependency_health_id: str
    service_id: str
    dependency_service_id: str
    status: ServiceHealthStatus
    reason_code: str
    observed_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"dependency_health_id": self.dependency_health_id, "service_id": self.service_id, "dependency_service_id": self.dependency_service_id, "reason_code": self.reason_code}, {"observed_at": self.observed_at})
        if not isinstance(self.status, ServiceHealthStatus):
            raise ValueError("status must be a ServiceHealthStatus")


@dataclass(frozen=True)
class OperationalHealthSnapshot:
    snapshot_id: str
    service_id: str
    status: ServiceHealthStatus
    sample_ids: tuple[str, ...]
    dependency_health_ids: tuple[str, ...]
    observed_at: str
    version: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"snapshot_id": self.snapshot_id, "service_id": self.service_id}, {"observed_at": self.observed_at}, self.version)
        _sorted_identifiers(self.sample_ids, "sample_id")
        _sorted_identifiers(self.dependency_health_ids, "dependency_health_id")
        if not isinstance(self.status, ServiceHealthStatus):
            raise ValueError("status must be a ServiceHealthStatus")


class SLOEvaluationState(str, Enum):
    GOOD = "good"
    BREACHED = "breached"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ServiceLevelIndicator:
    sli_id: str
    service_id: str
    indicator_type: str
    good_metric_id: str
    total_metric_id: str
    latency_threshold_ms: int | None
    created_at: str
    version: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"sli_id": self.sli_id, "service_id": self.service_id, "indicator_type": self.indicator_type, "good_metric_id": self.good_metric_id, "total_metric_id": self.total_metric_id}, {"created_at": self.created_at}, self.version)
        if self.latency_threshold_ms is not None:
            _integer(self.latency_threshold_ms, "latency_threshold_ms", 1)


@dataclass(frozen=True)
class SLOTarget:
    target_basis_points: int
    minimum_completeness_basis_points: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported SLO target schema")
        for name, value in (("target_basis_points", self.target_basis_points), ("minimum_completeness_basis_points", self.minimum_completeness_basis_points)):
            _integer(value, name)
            if value > 10_000:
                raise ValueError(f"{name} cannot exceed 10000")


@dataclass(frozen=True)
class ServiceLevelObjective:
    slo_id: str
    tenant_id: str
    service_id: str
    sli_id: str
    target: SLOTarget
    window_seconds: int
    created_at: str
    version: int
    active: bool = True
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"slo_id": self.slo_id, "tenant_id": self.tenant_id, "service_id": self.service_id, "sli_id": self.sli_id}, {"created_at": self.created_at}, self.version)
        _integer(self.window_seconds, "window_seconds", 1)
        if not isinstance(self.target, SLOTarget) or not isinstance(self.active, bool):
            raise ValueError("SLO target and active flag must use canonical types")


@dataclass(frozen=True)
class SLOEvaluation:
    evaluation_id: str
    slo_id: str
    tenant_id: str
    window_start: str
    window_end: str
    state: SLOEvaluationState
    achieved_basis_points: int | None
    completeness_basis_points: int
    good_count: int
    total_count: int
    excluded_maintenance_window_ids: tuple[str, ...]
    evaluated_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"evaluation_id": self.evaluation_id, "slo_id": self.slo_id, "tenant_id": self.tenant_id}, {"window_start": self.window_start, "window_end": self.window_end, "evaluated_at": self.evaluated_at})
        if _utc(self.window_start, "window_start") >= _utc(self.window_end, "window_end"):
            raise ValueError("SLO evaluation window must be positive")
        for name, value in (("completeness_basis_points", self.completeness_basis_points), ("good_count", self.good_count), ("total_count", self.total_count)):
            _integer(value, name)
        if self.achieved_basis_points is not None:
            _integer(self.achieved_basis_points, "achieved_basis_points")
        if not isinstance(self.state, SLOEvaluationState):
            raise ValueError("state must be a SLOEvaluationState")
        _sorted_identifiers(self.excluded_maintenance_window_ids, "maintenance_window_id")


@dataclass(frozen=True)
class ErrorBudget:
    error_budget_id: str
    evaluation_id: str
    slo_id: str
    allowed_bad_events: int
    consumed_bad_events: int
    remaining_bad_events: int
    calculated_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"error_budget_id": self.error_budget_id, "evaluation_id": self.evaluation_id, "slo_id": self.slo_id}, {"calculated_at": self.calculated_at})
        for name, value in (("allowed_bad_events", self.allowed_bad_events), ("consumed_bad_events", self.consumed_bad_events), ("remaining_bad_events", self.remaining_bad_events)):
            _integer(value, name)
        if self.remaining_bad_events != max(0, self.allowed_bad_events - self.consumed_bad_events):
            raise ValueError("remaining error budget must be clamped deterministically at zero")


@dataclass(frozen=True)
class BurnRateWindow:
    window_id: str
    duration_seconds: int
    threshold_microunits: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"window_id": self.window_id})
        _integer(self.duration_seconds, "duration_seconds", 1)
        _integer(self.threshold_microunits, "threshold_microunits")


@dataclass(frozen=True)
class BurnRateEvaluation:
    evaluation_id: str
    slo_id: str
    short_window: BurnRateWindow
    long_window: BurnRateWindow
    short_rate_microunits: int
    long_rate_microunits: int
    breached: bool
    evaluated_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"evaluation_id": self.evaluation_id, "slo_id": self.slo_id}, {"evaluated_at": self.evaluated_at})
        _integer(self.short_rate_microunits, "short_rate_microunits")
        _integer(self.long_rate_microunits, "long_rate_microunits")
        if not isinstance(self.short_window, BurnRateWindow) or not isinstance(self.long_window, BurnRateWindow) or not isinstance(self.breached, bool):
            raise ValueError("burn-rate evaluation uses invalid canonical values")


class AlertStatus(str, Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    SUPPRESSED = "suppressed"


@dataclass(frozen=True)
class AlertRule:
    rule_id: str
    tenant_id: str
    rule_type: str
    threshold: int
    active: bool
    created_at: str
    updated_at: str
    version: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"rule_id": self.rule_id, "tenant_id": self.tenant_id, "rule_type": self.rule_type}, {"created_at": self.created_at, "updated_at": self.updated_at}, self.version)
        _integer(self.threshold, "threshold")
        if not isinstance(self.active, bool):
            raise ValueError("active must be boolean")


@dataclass(frozen=True)
class AlertOccurrence:
    alert_id: str
    rule_id: str
    tenant_id: str
    deduplication_key: str
    status: AlertStatus
    reason_code: str
    evidence_reference: str
    opened_at: str
    updated_at: str
    version: int
    suppression_reason_code: str | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        identifiers = {"alert_id": self.alert_id, "rule_id": self.rule_id, "tenant_id": self.tenant_id, "deduplication_key": self.deduplication_key, "reason_code": self.reason_code, "evidence_reference": self.evidence_reference}
        if self.suppression_reason_code is not None:
            identifiers["suppression_reason_code"] = self.suppression_reason_code
        _base(self.schema_version, identifiers, {"opened_at": self.opened_at, "updated_at": self.updated_at}, self.version)
        if not isinstance(self.status, AlertStatus):
            raise ValueError("status must be an AlertStatus")
        if self.status is AlertStatus.SUPPRESSED and self.suppression_reason_code is None:
            raise ValueError("suppressed alert requires evidence")


class IncidentSeverity(str, Enum):
    SEV1 = "sev1"
    SEV2 = "sev2"
    SEV3 = "sev3"
    SEV4 = "sev4"


class IncidentStatus(str, Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    MITIGATING = "mitigating"
    RESOLVED = "resolved"
    CLOSED = "closed"


@dataclass(frozen=True)
class Incident:
    incident_id: str
    tenant_id: str
    platform_scoped: bool
    severity: IncidentSeverity
    status: IncidentStatus
    reason_code: str
    escalation_key: str
    owner_subject: str | None
    related_alert_ids: tuple[str, ...]
    related_runbook_execution_ids: tuple[str, ...]
    opened_at: str
    updated_at: str
    version: int
    parent_incident_id: str | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        identifiers = {"incident_id": self.incident_id, "tenant_id": self.tenant_id, "reason_code": self.reason_code, "escalation_key": self.escalation_key}
        for name, value in (("owner_subject", self.owner_subject), ("parent_incident_id", self.parent_incident_id)):
            if value is not None:
                identifiers[name] = value
        _base(self.schema_version, identifiers, {"opened_at": self.opened_at, "updated_at": self.updated_at}, self.version)
        _sorted_identifiers(self.related_alert_ids, "alert_id")
        _sorted_identifiers(self.related_runbook_execution_ids, "runbook_execution_id")
        if not isinstance(self.platform_scoped, bool) or not isinstance(self.severity, IncidentSeverity) or not isinstance(self.status, IncidentStatus):
            raise ValueError("incident scope, severity, and status must use canonical values")


@dataclass(frozen=True)
class IncidentTimelineEntry:
    entry_id: str
    incident_id: str
    tenant_id: str
    actor_subject: str
    entry_type: str
    reason_code: str
    occurred_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"entry_id": self.entry_id, "incident_id": self.incident_id, "tenant_id": self.tenant_id, "actor_subject": self.actor_subject, "entry_type": self.entry_type, "reason_code": self.reason_code}, {"occurred_at": self.occurred_at})


@dataclass(frozen=True)
class IncidentAssignment:
    assignment_id: str
    incident_id: str
    tenant_id: str
    owner_subject: str
    assigned_by: str
    assigned_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"assignment_id": self.assignment_id, "incident_id": self.incident_id, "tenant_id": self.tenant_id, "owner_subject": self.owner_subject, "assigned_by": self.assigned_by}, {"assigned_at": self.assigned_at})


class MaintenanceWindowStatus(str, Enum):
    REQUESTED = "requested"
    APPROVED = "approved"
    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class MaintenanceWindow:
    maintenance_window_id: str
    tenant_id: str
    service_ids: tuple[str, ...]
    environment: PolicyEnvironment
    status: MaintenanceWindowStatus
    reason_code: str
    requested_by: str
    approved_by: str | None
    starts_at: str
    ends_at: str
    created_at: str
    version: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        identifiers = {"maintenance_window_id": self.maintenance_window_id, "tenant_id": self.tenant_id, "reason_code": self.reason_code, "requested_by": self.requested_by}
        if self.approved_by is not None:
            identifiers["approved_by"] = self.approved_by
        _base(self.schema_version, identifiers, {"starts_at": self.starts_at, "ends_at": self.ends_at, "created_at": self.created_at}, self.version)
        _sorted_identifiers(self.service_ids, "service_id")
        if _utc(self.starts_at, "starts_at") >= _utc(self.ends_at, "ends_at"):
            raise ValueError("maintenance start must precede end")
        if not isinstance(self.environment, PolicyEnvironment) or not isinstance(self.status, MaintenanceWindowStatus):
            raise ValueError("maintenance environment and status must use canonical values")
        if self.environment is PolicyEnvironment.PRODUCTION and self.approved_by == self.requested_by:
            raise ValueError("production maintenance requires a distinct approver")


class RunbookStepType(str, Enum):
    MANUAL_CHECK = "manual_check"
    READ_ONLY_QUERY = "read_only_query"
    APPROVAL = "approval"
    COMMUNICATION = "communication"
    MITIGATION_INSTRUCTION = "mitigation_instruction"
    VERIFICATION = "verification"
    ESCALATION = "escalation"


class RunbookExecutionStatus(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABORTED = "aborted"


class RunbookStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    RETIRED = "retired"


PROHIBITED_RUNBOOK_TERMS = frozenset({"#!", "sudo ", "rm ", "curl ", "wget ", "kubectl ", "terraform ", "powershell", "select ", "insert ", "update ", "delete ", "drop ", "exec(", "subprocess", "script", "shell command"})


@dataclass(frozen=True)
class Runbook:
    runbook_id: str
    tenant_id: str
    name: str
    status: RunbookStatus
    active_version: int | None
    created_at: str
    updated_at: str
    version: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"runbook_id": self.runbook_id, "tenant_id": self.tenant_id}, {"created_at": self.created_at, "updated_at": self.updated_at}, self.version)
        _text(self.name, "name", 256)
        if self.active_version is not None:
            _integer(self.active_version, "active_version", 1)
        if not isinstance(self.status, RunbookStatus):
            raise ValueError("status must be a RunbookStatus")


@dataclass(frozen=True)
class RunbookStep:
    step_id: str
    runbook_id: str
    version_number: int
    order: int
    step_type: RunbookStepType
    instruction: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"step_id": self.step_id, "runbook_id": self.runbook_id})
        _integer(self.version_number, "version_number", 1)
        _integer(self.order, "order", 1)
        _text(self.instruction, "instruction", 2048)
        lowered = self.instruction.lower()
        if any(term in lowered for term in PROHIBITED_RUNBOOK_TERMS):
            raise ValueError("runbook instruction contains executable or prohibited content")
        if not isinstance(self.step_type, RunbookStepType):
            raise ValueError("step_type must be a RunbookStepType")


@dataclass(frozen=True)
class RunbookVersion:
    runbook_version_id: str
    runbook_id: str
    tenant_id: str
    version_number: int
    steps: tuple[RunbookStep, ...]
    created_by: str
    created_at: str
    digest: ConfigurationDigest
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"runbook_version_id": self.runbook_version_id, "runbook_id": self.runbook_id, "tenant_id": self.tenant_id, "created_by": self.created_by}, {"created_at": self.created_at})
        _integer(self.version_number, "version_number", 1)
        orders = tuple(item.order for item in self.steps)
        if orders != tuple(range(1, len(self.steps) + 1)):
            raise ValueError("runbook steps must be contiguous and ordered")
        if self.digest.digest != hashlib.sha256(operations_json(self.steps).encode()).hexdigest():
            raise ValueError("runbook digest does not match canonical steps")


@dataclass(frozen=True)
class RunbookExecution:
    execution_id: str
    runbook_id: str
    runbook_version_id: str
    tenant_id: str
    incident_id: str | None
    status: RunbookExecutionStatus
    next_step_order: int
    started_by: str
    started_at: str
    updated_at: str
    version: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        identifiers = {"execution_id": self.execution_id, "runbook_id": self.runbook_id, "runbook_version_id": self.runbook_version_id, "tenant_id": self.tenant_id, "started_by": self.started_by}
        if self.incident_id is not None:
            identifiers["incident_id"] = self.incident_id
        _base(self.schema_version, identifiers, {"started_at": self.started_at, "updated_at": self.updated_at}, self.version)
        _integer(self.next_step_order, "next_step_order", 1)
        if not isinstance(self.status, RunbookExecutionStatus):
            raise ValueError("status must be a RunbookExecutionStatus")


@dataclass(frozen=True)
class RunbookStepResult:
    result_id: str
    execution_id: str
    step_id: str
    tenant_id: str
    order: int
    outcome: str
    reason_code: str
    recorded_by: str
    recorded_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _base(self.schema_version, {"result_id": self.result_id, "execution_id": self.execution_id, "step_id": self.step_id, "tenant_id": self.tenant_id, "outcome": self.outcome, "reason_code": self.reason_code, "recorded_by": self.recorded_by}, {"recorded_at": self.recorded_at})
        _integer(self.order, "order", 1)
