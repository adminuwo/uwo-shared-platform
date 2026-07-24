"""Operational telemetry, SLO, alert, incident, runbook, and maintenance service."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any, Callable, Mapping

from packages.contracts import (
    AlertOccurrence, AlertRule, AlertStatus, BurnRateEvaluation, BurnRateWindow,
    ConfigurationDigest, DependencyHealth, ErrorBudget, HistogramBucket, Incident,
    IncidentAssignment, IncidentSeverity, IncidentStatus, IncidentTimelineEntry,
    MaintenanceWindow, MaintenanceWindowStatus, MetricKind, MetricSample,
    OperationalHealthSnapshot, OperationalMetric, Permission, PolicyEnvironment,
    Runbook, RunbookExecution, RunbookExecutionStatus, RunbookStatus, RunbookStep,
    RunbookStepResult, RunbookVersion, SLOEvaluation, SLOEvaluationState, SLOTarget,
    ServiceHealthStatus, ServiceIdentity, ServiceLevelIndicator, ServiceLevelObjective,
    TelemetryCheckpoint, VerifiedSubjectIdentity, operations_fingerprint, operations_json, utc_now,
)
from services.data_service_common import (
    AuditSink, Conflict, DataServiceAuthorizer, InvalidRequest, OutboxRecord, OutboxStatus,
    PolicyViolation, ResourceNotFound, ServiceAuditEvent, deterministic_id, platform_event,
)
from .repositories import OperationsIdempotencyRecord, Page, TelemetryExporter, UnitOfWorkFactory

ALERT_RULE_TYPES = frozenset({
    "slo-breach", "burn-rate-threshold", "service-unhealthy", "dependency-unavailable",
    "audit-integrity-failure", "outbox-dead-letter-threshold", "billing-compensation-failure-threshold",
})
SLI_TYPES = frozenset({
    "availability", "successful-request-ratio", "latency-threshold-compliance", "provider-execution-success",
    "billing-capture-success", "notification-delivery-success", "storage-integrity-success",
    "audit-chain-verification-success", "outbox-dispatch-success",
})
INCIDENT_TRANSITIONS = {
    IncidentStatus.OPEN: IncidentStatus.ACKNOWLEDGED,
    IncidentStatus.ACKNOWLEDGED: IncidentStatus.MITIGATING,
    IncidentStatus.MITIGATING: IncidentStatus.RESOLVED,
    IncidentStatus.RESOLVED: IncidentStatus.CLOSED,
}
TERMINAL_RUNBOOK = frozenset({RunbookExecutionStatus.COMPLETED, RunbookExecutionStatus.ABORTED})


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed): raise ValueError("timestamp must be UTC")
    return parsed


class PlatformOperationsService:
    def __init__(
        self,
        uow: UnitOfWorkFactory,
        authorizer: DataServiceAuthorizer,
        audit: AuditSink,
        exporter: TelemetryExporter,
        *,
        clock: Callable[[], str] = utc_now,
        past_lateness_seconds: int = 86_400,
        future_skew_seconds: int = 300,
        maximum_maintenance_seconds: int = 604_800,
    ) -> None:
        self._uow = uow; self._auth = authorizer; self._audit = audit; self._exporter = exporter; self._clock = clock
        self._late = past_lateness_seconds; self._future = future_skew_seconds; self._max_maintenance = maximum_maintenance_seconds

    @staticmethod
    def _page(values, limit, cursor):
        if limit < 1 or limit > 100: raise InvalidRequest("invalid_page_limit", "limit must be between 1 and 100")
        start = int(cursor or 0); items = tuple(values[start:start + limit]); return Page(items, str(start + limit) if start + limit < len(values) else None)

    @staticmethod
    def _require_key(key):
        if not isinstance(key, str) or not key or len(key) > 128: raise InvalidRequest("invalid_idempotency_key", "idempotency key must contain 1 to 128 characters")

    def _event(self, tx, event_type, tenant_id, request_id, resource_id, now, **attributes):
        values = {"resource_id": resource_id, **attributes}
        event = platform_event(event_type, tenant_id, request_id, values, now)
        tx.outbox.enqueue(OutboxRecord(deterministic_id("outbox", event.event_id), event, OutboxStatus.PENDING, 0, now, 1))

    def register_service_identity(self, identity, service_id, component_id, environment, region, request_id):
        self._auth.require_platform_admin(identity); now = self._clock()
        value = ServiceIdentity(service_id, component_id, PolicyEnvironment(environment), region, now, 1)
        with self._uow() as tx: result = tx.service_identities.create(value); tx.commit(); return result

    def register_metric(self, identity, tenant_id, service_id, metric_id, name, kind, unit, monotonic, histogram_bounds, request_id):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_SLO_MANAGE)
        now = self._clock()
        with self._uow() as tx:
            if tx.service_identities.get(service_id) is None: raise ResourceNotFound("unknown_service_identity", "service identity is not allowlisted")
            metric = OperationalMetric(metric_id, service_id, name, MetricKind(kind), unit, monotonic, tuple(histogram_bounds), now, 1)
            result = tx.metric_definitions.create(metric); tx.commit(); return result

    def _validate_sample_time(self, observed_at):
        observed = _dt(observed_at); now = _dt(self._clock())
        if observed < now - timedelta(seconds=self._late): raise PolicyViolation("telemetry_too_late", "telemetry exceeds the lateness window")
        if observed > now + timedelta(seconds=self._future): raise PolicyViolation("telemetry_future_skew", "telemetry exceeds future clock skew")

    def ingest_metric_sample(self, identity, tenant_id, service_id, metric_id, source_sample_id, observed_at, value, buckets, metadata, request_id):
        self._auth.require_executor(identity, tenant_id, allow_suspended=True); self._validate_sample_time(observed_at)
        parsed_buckets = tuple(HistogramBucket(item["upper_bound"], item["count"]) for item in buckets)
        sample_id = deterministic_id("metric-sample", service_id, source_sample_id)
        sample = MetricSample(sample_id, metric_id, service_id, observed_at, value, parsed_buckets, metadata)
        with self._uow() as tx:
            service = tx.service_identities.get(service_id); metric = tx.metric_definitions.get(metric_id)
            if service is None: raise ResourceNotFound("unknown_service_identity", "service identity is not allowlisted")
            if metric is None or metric.service_id != service_id: raise ResourceNotFound("unknown_metric", "metric is not registered for service")
            if metadata.get("tenant_id") not in {None, tenant_id}: raise PolicyViolation("tenant_isolation_violation", "telemetry tenant does not match verified scope")
            if metric.kind is MetricKind.HISTOGRAM:
                if tuple(item.upper_bound for item in parsed_buckets) != metric.histogram_bounds: raise PolicyViolation("invalid_histogram", "histogram buckets do not match the metric definition")
                if any(parsed_buckets[index].count > parsed_buckets[index + 1].count for index in range(len(parsed_buckets) - 1)): raise PolicyViolation("invalid_histogram", "histogram cumulative counts must be monotonic")
            elif parsed_buckets: raise PolicyViolation("invalid_histogram", "non-histogram metric cannot contain buckets")
            prior = sorted((item for item in tx.metric_samples.list() if item.metric_id == metric_id and item.service_id == service_id and item.observed_at <= observed_at), key=lambda item: (item.observed_at, item.sample_id))
            if metric.monotonic and prior and value < prior[-1].value: raise PolicyViolation("counter_regression", "monotonic counter cannot decrease")
            result = tx.metric_samples.append(sample); tx.commit()
        try: self._exporter.export_sample(result)
        except Exception: pass
        return result

    def ingest_dependency_health(self, identity, tenant_id, service_id, dependency_service_id, status, reason_code, observed_at, source_id):
        self._auth.require_executor(identity, tenant_id, allow_suspended=True); self._validate_sample_time(observed_at)
        value = DependencyHealth(deterministic_id("dependency-health", service_id, source_id), service_id, dependency_service_id, ServiceHealthStatus(status), reason_code, observed_at)
        with self._uow() as tx:
            if tx.service_identities.get(service_id) is None or tx.service_identities.get(dependency_service_id) is None: raise ResourceNotFound("unknown_service_identity", "service dependency is not allowlisted")
            result = tx.dependency_health.append(value); tx.commit(); return result

    def query_samples(self, identity, tenant_id, metric_id, start, end, limit=100, cursor=None):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_TELEMETRY_READ, allow_suspended=True)
        if _dt(start) >= _dt(end): raise InvalidRequest("invalid_time_window", "start must precede end")
        with self._uow() as tx:
            values = sorted((item for item in tx.metric_samples.list() if item.metric_id == metric_id and _dt(start) <= _dt(item.observed_at) < _dt(end)), key=lambda item: (item.observed_at, item.sample_id)); tx.commit()
        return self._page(values, limit, cursor)

    def list_dependencies(self, identity, tenant_id, service_id):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_TELEMETRY_READ, allow_suspended=True)
        with self._uow() as tx:
            values = tuple(sorted((item for item in tx.dependency_health.list() if item.service_id == service_id), key=lambda item: (item.observed_at, item.dependency_health_id))); tx.commit(); return values

    def latest_service_health(self, identity, tenant_id, service_id):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_TELEMETRY_READ, allow_suspended=True)
        with self._uow() as tx:
            snapshots = sorted((item for item in tx.health_snapshots.list() if item.service_id == service_id), key=lambda item: (item.observed_at, item.snapshot_id)); tx.commit()
        if snapshots: return snapshots[-1]
        now = self._clock(); return OperationalHealthSnapshot(deterministic_id("health-snapshot", service_id, "unknown", now), service_id, ServiceHealthStatus.UNKNOWN, (), (), now, 1)

    def create_health_snapshot(self, identity, tenant_id, service_id, request_id):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_TELEMETRY_READ, allow_suspended=True); now = self._clock()
        with self._uow() as tx:
            if tx.service_identities.get(service_id) is None: raise ResourceNotFound("unknown_service_identity", "service identity is not allowlisted")
            samples = tuple(item for item in tx.metric_samples.list() if item.service_id == service_id)
            dependencies = tuple(item for item in tx.dependency_health.list() if item.service_id == service_id)
            latest_dependencies = {}
            for item in sorted(dependencies, key=lambda value: (value.observed_at, value.dependency_health_id)): latest_dependencies[item.dependency_service_id] = item
            if not samples: status = ServiceHealthStatus.UNKNOWN
            elif any(item.status is ServiceHealthStatus.UNHEALTHY for item in latest_dependencies.values()): status = ServiceHealthStatus.UNHEALTHY
            elif any(item.status in {ServiceHealthStatus.DEGRADED, ServiceHealthStatus.UNKNOWN} for item in latest_dependencies.values()): status = ServiceHealthStatus.DEGRADED
            else: status = ServiceHealthStatus.HEALTHY
            value = OperationalHealthSnapshot(deterministic_id("health-snapshot", service_id, now), service_id, status, tuple(sorted(item.sample_id for item in samples)), tuple(sorted(item.dependency_health_id for item in latest_dependencies.values())), now, 1)
            result = tx.health_snapshots.append(value); tx.commit()
        try: self._exporter.export_health(result)
        except Exception: pass
        return result

    def create_telemetry_checkpoint(self, identity, tenant_id, service_id, request_id):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_TELEMETRY_READ, allow_suspended=True); now = self._clock()
        with self._uow() as tx:
            samples = tuple(sorted((item for item in tx.metric_samples.list() if item.service_id == service_id), key=lambda item: (item.observed_at, item.sample_id)))
            if not samples: raise PolicyViolation("missing_telemetry", "cannot checkpoint missing telemetry")
            digest = ConfigurationDigest("sha256", hashlib.sha256(operations_json(samples).encode()).hexdigest())
            value = TelemetryCheckpoint(deterministic_id("telemetry-checkpoint", service_id, samples[-1].sample_id), service_id, samples[-1].sample_id, len(samples), digest, now)
            result = tx.telemetry_checkpoints.append(value); tx.commit(); return result

    def create_sli(self, identity, tenant_id, service_id, indicator_type, good_metric_id, total_metric_id, latency_threshold_ms, request_id):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_SLO_MANAGE)
        if indicator_type not in SLI_TYPES: raise InvalidRequest("unknown_sli_type", "SLI type is not supported")
        now = self._clock(); value = ServiceLevelIndicator(deterministic_id("sli", tenant_id, service_id, indicator_type, good_metric_id, total_metric_id), service_id, indicator_type, good_metric_id, total_metric_id, latency_threshold_ms, now, 1)
        with self._uow() as tx: result = tx.sli_definitions.create(value); tx.commit(); return result

    def create_slo(self, identity, tenant_id, service_id, sli_id, target_basis_points, minimum_completeness_basis_points, window_seconds, request_id):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_SLO_MANAGE); now = self._clock()
        with self._uow() as tx:
            if tx.sli_definitions.get(sli_id) is None: raise ResourceNotFound("unknown_sli", "SLI does not exist")
            value = ServiceLevelObjective(deterministic_id("slo", tenant_id, service_id, sli_id, window_seconds), tenant_id, service_id, sli_id, SLOTarget(target_basis_points, minimum_completeness_basis_points), window_seconds, now, 1)
            result = tx.slo_definitions.create(value); tx.commit(); return result

    @staticmethod
    def _maintenance_for(tx, tenant_id, service_id, start, end):
        return tuple(sorted((window for window in tx.maintenance_windows.list() if window.tenant_id == tenant_id and service_id in window.service_ids and window.status in {MaintenanceWindowStatus.APPROVED, MaintenanceWindowStatus.ACTIVE, MaintenanceWindowStatus.EXPIRED} and _dt(window.starts_at) < _dt(end) and _dt(window.ends_at) > _dt(start)), key=lambda item: item.maintenance_window_id))

    def evaluate_slo(self, identity, tenant_id, slo_id, window_start, window_end, request_id):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_SLO_MANAGE, allow_suspended=True)
        if _dt(window_start) >= _dt(window_end): raise InvalidRequest("invalid_time_window", "SLO window must be positive")
        with self._uow() as tx:
            slo = tx.slo_definitions.get(slo_id)
            if slo is None or slo.tenant_id != tenant_id: raise ResourceNotFound("unknown_slo", "SLO does not exist")
            sli = tx.sli_definitions.get(slo.sli_id)
            maintenance = self._maintenance_for(tx, tenant_id, slo.service_id, window_start, window_end)
            suppressible = sli.indicator_type != "audit-chain-verification-success"
            def included(sample):
                if not (_dt(window_start) <= _dt(sample.observed_at) < _dt(window_end)): return False
                return not (suppressible and any(_dt(item.starts_at) <= _dt(sample.observed_at) < _dt(item.ends_at) for item in maintenance))
            good_samples = [item for item in tx.metric_samples.list() if item.metric_id == sli.good_metric_id and included(item)]
            total_samples = [item for item in tx.metric_samples.list() if item.metric_id == sli.total_metric_id and included(item)]
            good = sum(item.value for item in good_samples); total = sum(item.value for item in total_samples)
            paired_samples = min(len(good_samples), len(total_samples)); expected_samples = max(len(good_samples), len(total_samples), 1)
            completeness = paired_samples * 10_000 // expected_samples
            if good > total: raise PolicyViolation("invalid_sli_counts", "SLI good count cannot exceed total count")
            achieved = (good * 10_000 // total) if total > 0 else None
            if achieved is None or completeness < slo.target.minimum_completeness_basis_points: state = SLOEvaluationState.UNKNOWN
            else: state = SLOEvaluationState.GOOD if achieved >= slo.target.target_basis_points else SLOEvaluationState.BREACHED
            value = SLOEvaluation(deterministic_id("slo-evaluation", slo_id, window_start, window_end), slo_id, tenant_id, window_start, window_end, state, achieved, completeness, good, total, tuple(item.maintenance_window_id for item in maintenance), window_end)
            existing = tx.slo_evaluations.get(value.evaluation_id)
            prior = sorted((item for item in tx.slo_evaluations.list() if item.slo_id == slo_id and item.window_end <= window_start), key=lambda item: (item.window_end, item.evaluation_id))
            result = existing if existing is not None else tx.slo_evaluations.append(value)
            if existing is not None and existing != value: raise Conflict("slo_evaluation_conflict", "historical SLO evaluation is immutable")
            if existing is None and state is SLOEvaluationState.BREACHED:
                self._event(tx, "slo.breached", tenant_id, request_id, value.evaluation_id, window_end)
                allowed_bad = total * (10_000 - slo.target.target_basis_points) // 10_000
                if total - good > 0 and total - good >= allowed_bad:
                    self._event(tx, "error_budget.exhausted", tenant_id, f"{request_id}-budget", value.evaluation_id, window_end)
                for rule in tx.alert_rules.list():
                    if rule.tenant_id != tenant_id or rule.rule_type != "slo-breach" or not rule.active: continue
                    deduplication_key = deterministic_id("alert-dedup", rule.rule_id, value.evaluation_id)
                    if any(item.deduplication_key == deduplication_key for item in tx.alert_occurrences.list()): continue
                    alert = AlertOccurrence(deterministic_id("alert", deduplication_key), rule.rule_id, tenant_id, deduplication_key, AlertStatus.OPEN, "slo-breach", value.evaluation_id, window_end, window_end, 1)
                    tx.alert_occurrences.create(alert)
                    self._event(tx, "alert.opened", tenant_id, request_id, alert.alert_id, window_end, reason_code="slo-breach")
            if existing is None and state is SLOEvaluationState.GOOD and prior and prior[-1].state is SLOEvaluationState.BREACHED:
                self._event(tx, "slo.recovered", tenant_id, request_id, value.evaluation_id, window_end)
            tx.commit(); return result

    def error_budget(self, identity, tenant_id, evaluation_id):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_TELEMETRY_READ, allow_suspended=True)
        with self._uow() as tx:
            evaluation = tx.slo_evaluations.get(evaluation_id)
            if evaluation is None or evaluation.tenant_id != tenant_id: raise ResourceNotFound("unknown_slo_evaluation", "SLO evaluation does not exist")
            slo = tx.slo_definitions.get(evaluation.slo_id); tx.commit()
        allowed = evaluation.total_count * (10_000 - slo.target.target_basis_points) // 10_000; consumed = max(0, evaluation.total_count - evaluation.good_count)
        return ErrorBudget(deterministic_id("error-budget", evaluation_id), evaluation_id, slo.slo_id, allowed, consumed, max(0, allowed - consumed), evaluation.evaluated_at)

    def evaluate_burn_rate(self, identity, tenant_id, slo_id, short_window, long_window, end, request_id):
        short_start = (_dt(end) - timedelta(seconds=short_window.duration_seconds)).astimezone(timezone.utc).isoformat()
        long_start = (_dt(end) - timedelta(seconds=long_window.duration_seconds)).astimezone(timezone.utc).isoformat()
        short = self.evaluate_slo(identity, tenant_id, slo_id, short_start, end, f"{request_id}-short"); long = self.evaluate_slo(identity, tenant_id, slo_id, long_start, end, f"{request_id}-long")
        with self._uow() as tx: slo = tx.slo_definitions.get(slo_id); tx.commit()
        allowance = max(1, 10_000 - slo.target.target_basis_points)
        def rate(value): return 0 if value.total_count == 0 else ((value.total_count - value.good_count) * 10_000 * 1_000_000) // (value.total_count * allowance)
        short_rate, long_rate = rate(short), rate(long)
        return BurnRateEvaluation(deterministic_id("burn-rate", slo_id, short.evaluation_id, long.evaluation_id), slo_id, short_window, long_window, short_rate, long_rate, short_rate >= short_window.threshold_microunits and long_rate >= long_window.threshold_microunits, end)

    def create_alert_rule(self, identity, tenant_id, rule_type, threshold, request_id):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_ALERT_MANAGE)
        if rule_type not in ALERT_RULE_TYPES: raise InvalidRequest("unknown_alert_rule", "alert rule type is not supported")
        now = self._clock(); value = AlertRule(deterministic_id("alert-rule", tenant_id, rule_type), tenant_id, rule_type, threshold, True, now, now, 1)
        with self._uow() as tx: result = tx.alert_rules.create(value); tx.commit(); return result

    def set_alert_rule_active(self, identity, tenant_id, rule_id, active, expected_version):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_ALERT_MANAGE)
        with self._uow() as tx:
            rule = tx.alert_rules.get(rule_id)
            if rule is None or rule.tenant_id != tenant_id: raise ResourceNotFound("unknown_alert_rule", "alert rule does not exist")
            result = tx.alert_rules.update(replace(rule, active=active, updated_at=self._clock(), version=rule.version + 1), expected_version); tx.commit(); return result

    def evaluate_alert(self, identity, tenant_id, rule_id, evidence_reference, triggered, request_id):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_ALERT_MANAGE, allow_suspended=True); now = self._clock()
        with self._uow() as tx:
            rule = tx.alert_rules.get(rule_id)
            if rule is None or rule.tenant_id != tenant_id: raise ResourceNotFound("unknown_alert_rule", "alert rule does not exist")
            if not rule.active or not triggered: tx.commit(); return None
            deduplication_key = deterministic_id("alert-dedup", rule_id, evidence_reference)
            existing = next((item for item in tx.alert_occurrences.list() if item.deduplication_key == deduplication_key), None)
            if existing is not None: tx.commit(); return existing
            maintenance = tuple(item for item in tx.maintenance_windows.list() if item.tenant_id == tenant_id and item.status is MaintenanceWindowStatus.ACTIVE and _dt(item.starts_at) <= _dt(now) < _dt(item.ends_at))
            suppressed = bool(maintenance) and rule.rule_type != "audit-integrity-failure"
            status = AlertStatus.SUPPRESSED if suppressed else AlertStatus.OPEN
            suppression = "active-maintenance-window" if suppressed else None
            value = AlertOccurrence(deterministic_id("alert", deduplication_key), rule_id, tenant_id, deduplication_key, status, rule.rule_type, evidence_reference, now, now, 1, suppression)
            result = tx.alert_occurrences.create(value)
            if not suppressed: self._event(tx, "alert.opened", tenant_id, request_id, value.alert_id, now, reason_code=rule.rule_type)
            tx.commit(); return result

    def transition_alert(self, identity, tenant_id, alert_id, target_status, expected_version, request_id):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_ALERT_MANAGE, allow_suspended=True); target = AlertStatus(target_status); now = self._clock()
        with self._uow() as tx:
            alert = tx.alert_occurrences.get(alert_id)
            if alert is None or alert.tenant_id != tenant_id: raise ResourceNotFound("unknown_alert", "alert does not exist")
            allowed = {AlertStatus.OPEN: AlertStatus.ACKNOWLEDGED, AlertStatus.ACKNOWLEDGED: AlertStatus.RESOLVED}
            if allowed.get(alert.status) is not target: raise PolicyViolation("invalid_alert_transition", "alert transition is invalid")
            result = tx.alert_occurrences.update(replace(alert, status=target, updated_at=now, version=alert.version + 1), expected_version)
            self._event(tx, f"alert.{target.value}", tenant_id, request_id, alert_id, now); tx.commit(); return result

    def list_alerts(self, identity, tenant_id, limit=50, cursor=None):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_ALERT_MANAGE, allow_suspended=True)
        with self._uow() as tx: values = tuple(item for item in tx.alert_occurrences.list() if item.tenant_id == tenant_id); tx.commit()
        return self._page(values, limit, cursor)

    def create_incident(self, identity, tenant_id, severity, reason_code, escalation_key, platform_scoped, related_alert_ids, request_id):
        if platform_scoped: self._auth.require_platform_admin(identity)
        else: self._auth.require(identity, tenant_id, Permission.OPERATIONS_INCIDENT_MANAGE, allow_suspended=True)
        now = self._clock()
        with self._uow() as tx:
            existing = next((item for item in tx.incidents.list() if item.escalation_key == escalation_key and item.status is not IncidentStatus.CLOSED), None)
            if existing is not None: tx.commit(); return existing
            value = Incident(deterministic_id("incident", tenant_id, escalation_key), tenant_id, platform_scoped, IncidentSeverity(severity), IncidentStatus.OPEN, reason_code, escalation_key, None, tuple(sorted(set(related_alert_ids))), (), now, now, 1)
            result = tx.incidents.create(value)
            entry = IncidentTimelineEntry(deterministic_id("incident-entry", value.incident_id, 1), value.incident_id, tenant_id, identity.subject, "opened", reason_code, now)
            tx.incident_timeline.append(entry); self._event(tx, "incident.opened", tenant_id, request_id, value.incident_id, now, reason_code=reason_code); tx.commit(); return result

    def escalate_alert(self, identity, tenant_id, alert_id, severity, request_id):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_INCIDENT_MANAGE, allow_suspended=True)
        with self._uow() as tx:
            alert = tx.alert_occurrences.get(alert_id); tx.commit()
        if alert is None or alert.tenant_id != tenant_id: raise ResourceNotFound("unknown_alert", "alert does not exist")
        return self.create_incident(identity, tenant_id, severity, alert.reason_code, f"alert:{alert_id}", False, (alert_id,), request_id)

    def assign_incident(self, identity, tenant_id, incident_id, owner_subject, expected_version, request_id):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_INCIDENT_MANAGE, allow_suspended=True); now = self._clock()
        with self._uow() as tx:
            incident = tx.incidents.get(incident_id)
            if incident is None or incident.tenant_id != tenant_id: raise ResourceNotFound("unknown_incident", "incident does not exist")
            result = tx.incidents.update(replace(incident, owner_subject=owner_subject, updated_at=now, version=incident.version + 1), expected_version)
            tx.incident_timeline.append(IncidentTimelineEntry(deterministic_id("incident-entry", incident_id, result.version, "assigned"), incident_id, tenant_id, identity.subject, "assigned", "owner-assigned", now)); tx.commit()
        return IncidentAssignment(deterministic_id("incident-assignment", incident_id, result.version), incident_id, tenant_id, owner_subject, identity.subject, now)

    def transition_incident(self, identity, tenant_id, incident_id, target_status, reason_code, expected_version, request_id):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_INCIDENT_MANAGE, allow_suspended=True); target = IncidentStatus(target_status); now = self._clock()
        with self._uow() as tx:
            incident = tx.incidents.get(incident_id)
            if incident is None or incident.tenant_id != tenant_id: raise ResourceNotFound("unknown_incident", "incident does not exist")
            if INCIDENT_TRANSITIONS.get(incident.status) is not target: raise PolicyViolation("invalid_incident_transition", "incident transition is invalid")
            result = tx.incidents.update(replace(incident, status=target, updated_at=now, version=incident.version + 1), expected_version)
            tx.incident_timeline.append(IncidentTimelineEntry(deterministic_id("incident-entry", incident_id, result.version, target.value), incident_id, tenant_id, identity.subject, target.value, reason_code, now))
            if target in {IncidentStatus.RESOLVED, IncidentStatus.CLOSED}: self._event(tx, f"incident.{target.value}", tenant_id, request_id, incident_id, now, reason_code=reason_code)
            tx.commit(); return result

    def incident_history(self, identity, tenant_id, incident_id):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_INCIDENT_MANAGE, allow_suspended=True)
        with self._uow() as tx:
            incident = tx.incidents.get(incident_id); history = tuple(item for item in tx.incident_timeline.list() if item.incident_id == incident_id); tx.commit()
        if incident is None or incident.tenant_id != tenant_id: raise ResourceNotFound("unknown_incident", "incident does not exist")
        return {"incident": incident, "timeline": history}

    def reopen_incident(self, identity, tenant_id, incident_id, severity, reason_code, request_id):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_INCIDENT_MANAGE, allow_suspended=True); now = self._clock()
        with self._uow() as tx:
            parent = tx.incidents.get(incident_id)
            if parent is None or parent.tenant_id != tenant_id: raise ResourceNotFound("unknown_incident", "incident does not exist")
            if parent.status is not IncidentStatus.CLOSED: raise PolicyViolation("incident_not_closed", "only a closed incident can be related to a new incident")
            escalation_key = deterministic_id("incident-reopen-key", incident_id, request_id)
            value = Incident(deterministic_id("incident", tenant_id, escalation_key), tenant_id, parent.platform_scoped, IncidentSeverity(severity), IncidentStatus.OPEN, reason_code, escalation_key, None, (), (), now, now, 1, parent.incident_id)
            result = tx.incidents.create(value)
            tx.incident_timeline.append(IncidentTimelineEntry(deterministic_id("incident-entry", value.incident_id, 1), value.incident_id, tenant_id, identity.subject, "opened", reason_code, now))
            self._event(tx, "incident.opened", tenant_id, request_id, value.incident_id, now, reason_code=reason_code); tx.commit(); return result

    def list_incidents(self, identity, tenant_id, limit=50, cursor=None):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_INCIDENT_MANAGE, allow_suspended=True)
        with self._uow() as tx: values = tuple(item for item in tx.incidents.list() if item.tenant_id == tenant_id); tx.commit()
        return self._page(values, limit, cursor)

    def create_runbook(self, identity, tenant_id, name, request_id):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_RUNBOOK_MANAGE)
        now = self._clock(); value = Runbook(deterministic_id("runbook", tenant_id, name), tenant_id, name, RunbookStatus.DRAFT, None, now, now, 1)
        with self._uow() as tx: result = tx.runbooks.create(value); tx.commit(); return result

    def append_runbook_version(self, identity, tenant_id, runbook_id, steps, request_id):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_RUNBOOK_MANAGE); now = self._clock()
        with self._uow() as tx:
            runbook = tx.runbooks.get(runbook_id)
            if runbook is None or runbook.tenant_id != tenant_id: raise ResourceNotFound("unknown_runbook", "runbook does not exist")
            versions = [item for item in tx.runbook_versions.list() if item.runbook_id == runbook_id]; number = max((item.version_number for item in versions), default=0) + 1
            from packages.contracts import RunbookStepType
            parsed = tuple(RunbookStep(deterministic_id("runbook-step", runbook_id, number, index), runbook_id, number, index, RunbookStepType(item["step_type"]), item["instruction"]) for index, item in enumerate(steps, 1))
            digest = ConfigurationDigest("sha256", hashlib.sha256(operations_json(parsed).encode()).hexdigest())
            value = RunbookVersion(deterministic_id("runbook-version", runbook_id, number), runbook_id, tenant_id, number, parsed, identity.subject, now, digest)
            result = tx.runbook_versions.append(value); tx.commit(); return result

    def activate_runbook_version(self, identity, tenant_id, runbook_id, version_number, expected_version):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_RUNBOOK_MANAGE); now = self._clock()
        with self._uow() as tx:
            runbook = tx.runbooks.get(runbook_id); version = next((item for item in tx.runbook_versions.list() if item.runbook_id == runbook_id and item.version_number == version_number), None)
            if runbook is None or runbook.tenant_id != tenant_id or version is None: raise ResourceNotFound("unknown_runbook_version", "runbook version does not exist")
            result = tx.runbooks.update(replace(runbook, status=RunbookStatus.ACTIVE, active_version=version_number, updated_at=now, version=runbook.version + 1), expected_version); tx.commit(); return result

    def retire_runbook(self, identity, tenant_id, runbook_id, expected_version):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_RUNBOOK_MANAGE); now = self._clock()
        with self._uow() as tx:
            runbook = tx.runbooks.get(runbook_id)
            if runbook is None or runbook.tenant_id != tenant_id: raise ResourceNotFound("unknown_runbook", "runbook does not exist")
            result = tx.runbooks.update(replace(runbook, status=RunbookStatus.RETIRED, updated_at=now, version=runbook.version + 1), expected_version); tx.commit(); return result

    def read_runbook(self, identity, tenant_id, runbook_id):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_RUNBOOK_READ, allow_suspended=True)
        with self._uow() as tx:
            runbook = tx.runbooks.get(runbook_id); versions = tuple(item for item in tx.runbook_versions.list() if item.runbook_id == runbook_id); tx.commit()
        if runbook is None or runbook.tenant_id != tenant_id: raise ResourceNotFound("unknown_runbook", "runbook does not exist")
        return {"runbook": runbook, "versions": versions}

    def validate_runbook_version(self, identity, tenant_id, runbook_version_id):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_RUNBOOK_READ, allow_suspended=True)
        with self._uow() as tx: version = tx.runbook_versions.get(runbook_version_id); tx.commit()
        if version is None or version.tenant_id != tenant_id: raise ResourceNotFound("unknown_runbook_version", "runbook version does not exist")
        expected = hashlib.sha256(operations_json(version.steps).encode()).hexdigest()
        return {"runbook_version_id": runbook_version_id, "valid": version.digest.digest == expected, "digest": expected}

    def start_runbook_execution(self, identity, tenant_id, runbook_id, incident_id, idempotency_key, request_id):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_RUNBOOK_MANAGE, allow_suspended=True); self._require_key(idempotency_key); now = self._clock()
        operation = "start-runbook"; fingerprint = operations_fingerprint({"runbook": runbook_id, "incident": incident_id})
        with self._uow() as tx:
            replay = tx.idempotency.get(operation, tenant_id, identity.subject, idempotency_key)
            if replay is not None:
                if replay.request_fingerprint != fingerprint: raise Conflict("idempotency_conflict", "idempotency key has conflicting content")
                tx.commit(); return replay.original_result
            runbook = tx.runbooks.get(runbook_id)
            if runbook is None or runbook.tenant_id != tenant_id or runbook.status is not RunbookStatus.ACTIVE: raise PolicyViolation("runbook_not_active", "active runbook is required")
            version = next(item for item in tx.runbook_versions.list() if item.runbook_id == runbook_id and item.version_number == runbook.active_version)
            value = RunbookExecution(deterministic_id("runbook-execution", tenant_id, runbook_id, idempotency_key), runbook_id, version.runbook_version_id, tenant_id, incident_id, RunbookExecutionStatus.RUNNING, 1, identity.subject, now, now, 1)
            result = tx.runbook_executions.create(value); tx.idempotency.put(OperationsIdempotencyRecord(operation, tenant_id, identity.subject, idempotency_key, fingerprint, result))
            self._event(tx, "runbook.started", tenant_id, request_id, result.execution_id, now); tx.commit(); return result

    def record_runbook_step(self, identity, tenant_id, execution_id, step_id, outcome, reason_code, request_id):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_RUNBOOK_MANAGE, allow_suspended=True); now = self._clock()
        with self._uow() as tx:
            execution = tx.runbook_executions.get(execution_id)
            if execution is None or execution.tenant_id != tenant_id: raise ResourceNotFound("unknown_runbook_execution", "runbook execution does not exist")
            if execution.status is not RunbookExecutionStatus.RUNNING: raise PolicyViolation("runbook_execution_not_running", "runbook execution is not running")
            version = tx.runbook_versions.get(execution.runbook_version_id); step = next((item for item in version.steps if item.step_id == step_id), None)
            if step is None: raise ResourceNotFound("unknown_runbook_step", "runbook step does not exist")
            result_id = deterministic_id("runbook-step-result", execution_id, step_id); existing = tx.runbook_step_results.get(result_id)
            if existing is not None:
                if (existing.outcome, existing.reason_code, existing.recorded_by) != (outcome, reason_code, identity.subject): raise Conflict("runbook_step_result_conflict", "runbook step retry has conflicting content")
                tx.commit(); return existing
            if step.order != execution.next_step_order: raise PolicyViolation("runbook_step_out_of_order", "runbook steps must be recorded in order")
            result = RunbookStepResult(result_id, execution_id, step_id, tenant_id, step.order, outcome, reason_code, identity.subject, now)
            tx.runbook_step_results.append(result); tx.runbook_executions.update(replace(execution, next_step_order=execution.next_step_order + 1, updated_at=now, version=execution.version + 1), execution.version); tx.commit(); return result

    def transition_runbook_execution(self, identity, tenant_id, execution_id, target_status, expected_version, request_id):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_RUNBOOK_MANAGE, allow_suspended=True); target = RunbookExecutionStatus(target_status); now = self._clock()
        with self._uow() as tx:
            execution = tx.runbook_executions.get(execution_id)
            if execution is None or execution.tenant_id != tenant_id: raise ResourceNotFound("unknown_runbook_execution", "runbook execution does not exist")
            if execution.status in TERMINAL_RUNBOOK: raise PolicyViolation("runbook_execution_terminal", "terminal execution cannot be mutated")
            allowed = {(RunbookExecutionStatus.RUNNING, RunbookExecutionStatus.PAUSED), (RunbookExecutionStatus.PAUSED, RunbookExecutionStatus.RUNNING), (RunbookExecutionStatus.RUNNING, RunbookExecutionStatus.ABORTED), (RunbookExecutionStatus.PAUSED, RunbookExecutionStatus.ABORTED), (RunbookExecutionStatus.RUNNING, RunbookExecutionStatus.COMPLETED)}
            if (execution.status, target) not in allowed: raise PolicyViolation("invalid_runbook_transition", "runbook execution transition is invalid")
            if target is RunbookExecutionStatus.COMPLETED:
                version = tx.runbook_versions.get(execution.runbook_version_id)
                if execution.next_step_order <= len(version.steps): raise PolicyViolation("runbook_steps_incomplete", "all runbook steps must complete first")
            result = tx.runbook_executions.update(replace(execution, status=target, updated_at=now, version=execution.version + 1), expected_version)
            if target in TERMINAL_RUNBOOK: self._event(tx, f"runbook.{target.value}", tenant_id, request_id, execution_id, now)
            tx.commit(); return result

    def runbook_execution_history(self, identity, tenant_id, execution_id):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_RUNBOOK_READ, allow_suspended=True)
        with self._uow() as tx:
            execution = tx.runbook_executions.get(execution_id); results = tuple(item for item in tx.runbook_step_results.list() if item.execution_id == execution_id); tx.commit()
        if execution is None or execution.tenant_id != tenant_id: raise ResourceNotFound("unknown_runbook_execution", "runbook execution does not exist")
        return {"execution": execution, "results": results}

    def create_maintenance_window(self, identity, tenant_id, service_ids, environment, reason_code, starts_at, ends_at, request_id):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_MAINTENANCE_MANAGE, allow_suspended=True)
        if _dt(ends_at) - _dt(starts_at) > timedelta(seconds=self._max_maintenance): raise PolicyViolation("maintenance_duration_exceeded", "maintenance duration exceeds policy")
        now = self._clock(); value = MaintenanceWindow(deterministic_id("maintenance", tenant_id, service_ids, starts_at, ends_at), tenant_id, tuple(sorted(set(service_ids))), PolicyEnvironment(environment), MaintenanceWindowStatus.REQUESTED, reason_code, identity.subject, None, starts_at, ends_at, now, 1)
        with self._uow() as tx: result = tx.maintenance_windows.create(value); tx.commit(); return result

    def approve_maintenance_window(self, identity, tenant_id, window_id, expected_version, request_id):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_MAINTENANCE_MANAGE, allow_suspended=True); now = self._clock()
        with self._uow() as tx:
            window = tx.maintenance_windows.get(window_id)
            if window is None or window.tenant_id != tenant_id: raise ResourceNotFound("unknown_maintenance_window", "maintenance window does not exist")
            if window.status is not MaintenanceWindowStatus.REQUESTED: raise PolicyViolation("invalid_maintenance_transition", "maintenance is not awaiting approval")
            if window.environment is PolicyEnvironment.PRODUCTION and window.requested_by == identity.subject: raise PolicyViolation("self_approval_denied", "production maintenance requires a distinct approver")
            result = tx.maintenance_windows.update(replace(window, status=MaintenanceWindowStatus.APPROVED, approved_by=identity.subject, version=window.version + 1), expected_version); tx.commit(); return result

    def evaluate_maintenance_window(self, identity, tenant_id, window_id, expected_version, request_id):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_MAINTENANCE_MANAGE, allow_suspended=True); now = self._clock()
        with self._uow() as tx:
            window = tx.maintenance_windows.get(window_id)
            if window is None or window.tenant_id != tenant_id: raise ResourceNotFound("unknown_maintenance_window", "maintenance window does not exist")
            target = window.status
            if window.status is MaintenanceWindowStatus.APPROVED and _dt(window.starts_at) <= _dt(now) < _dt(window.ends_at): target = MaintenanceWindowStatus.ACTIVE
            if window.status in {MaintenanceWindowStatus.APPROVED, MaintenanceWindowStatus.ACTIVE} and _dt(now) >= _dt(window.ends_at): target = MaintenanceWindowStatus.EXPIRED
            if target is window.status: tx.commit(); return window
            result = tx.maintenance_windows.update(replace(window, status=target, version=window.version + 1), expected_version)
            event_type = "maintenance.started" if target is MaintenanceWindowStatus.ACTIVE else "maintenance.ended"
            self._event(tx, event_type, tenant_id, request_id, window_id, now, reason_code=window.reason_code); tx.commit(); return result

    def cancel_maintenance_window(self, identity, tenant_id, window_id, expected_version):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_MAINTENANCE_MANAGE, allow_suspended=True)
        with self._uow() as tx:
            window = tx.maintenance_windows.get(window_id)
            if window is None or window.tenant_id != tenant_id: raise ResourceNotFound("unknown_maintenance_window", "maintenance window does not exist")
            if window.status not in {MaintenanceWindowStatus.REQUESTED, MaintenanceWindowStatus.APPROVED}: raise PolicyViolation("invalid_maintenance_transition", "active or terminal maintenance cannot be cancelled")
            result = tx.maintenance_windows.update(replace(window, status=MaintenanceWindowStatus.CANCELLED, version=window.version + 1), expected_version); tx.commit(); return result

    def list_maintenance_windows(self, identity, tenant_id):
        self._auth.require(identity, tenant_id, Permission.OPERATIONS_TELEMETRY_READ, allow_suspended=True)
        with self._uow() as tx: values = tuple(item for item in tx.maintenance_windows.list() if item.tenant_id == tenant_id); tx.commit(); return values
