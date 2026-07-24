"""Thread-safe rollback-capable operations repositories for tests only."""

from __future__ import annotations

from dataclasses import fields
from threading import RLock

from services.data_service_common import Conflict, InMemoryOutbox, RepositoryIntegrityError

from .repositories import OperationsIdempotencyRecord


KEY_FIELDS = {
    "service_identities": "service_id", "metric_definitions": "metric_id", "metric_samples": "sample_id",
    "dependency_health": "dependency_health_id", "health_snapshots": "snapshot_id",
    "sli_definitions": "sli_id", "slo_definitions": "slo_id", "slo_evaluations": "evaluation_id",
    "alert_rules": "rule_id", "alert_occurrences": "alert_id", "incidents": "incident_id",
    "incident_timeline": "entry_id", "runbooks": "runbook_id", "runbook_versions": "runbook_version_id",
    "runbook_executions": "execution_id", "runbook_step_results": "result_id",
    "maintenance_windows": "maintenance_window_id", "telemetry_checkpoints": "checkpoint_id",
}
IMMUTABLE = frozenset({
    "metric_samples", "dependency_health", "health_snapshots", "slo_evaluations", "incident_timeline",
    "runbook_versions", "runbook_step_results", "telemetry_checkpoints",
})


class InMemoryOperationsState:
    def __init__(self) -> None:
        for name in KEY_FIELDS: setattr(self, name, {})
        self.idempotency = {}; self.outbox = InMemoryOutbox(); self.lock = RLock(); self.fail_next: str | None = None

    def fail(self, point: str) -> None:
        if self.fail_next == point:
            self.fail_next = None; raise RepositoryIntegrityError("injected operations repository failure")

    def snapshot(self):
        return ({name: dict(getattr(self, name)) for name in KEY_FIELDS}, dict(self.idempotency), self.outbox.snapshot())

    def restore(self, snapshot) -> None:
        values, self.idempotency, outbox = snapshot
        for name, items in values.items(): setattr(self, name, items)
        self.outbox.restore(outbox)


class _Repository:
    def __init__(self, state, name):
        self.s = state; self.name = name; self.key_field = KEY_FIELDS[name]; self.items = getattr(state, name)
    def _key(self, value): return getattr(value, self.key_field)
    def create(self, value): return self._insert(value)
    def append(self, value): return self._insert(value)
    def _insert(self, value):
        key = self._key(value); current = self.items.get(key)
        if current is not None:
            if current != value: raise Conflict(f"{self.name}_conflict", "immutable identifier has conflicting content")
            return current
        self.items[key] = value; self.s.fail(self.name); return value
    def get(self, key): return self.items.get(key)
    def update(self, value, expected_version):
        if self.name in IMMUTABLE: raise Conflict("immutable_record", "immutable record cannot be updated")
        key = self._key(value); current = self.items.get(key)
        if current is None: raise Conflict(f"unknown_{self.name}", "record does not exist")
        current_version = getattr(current, "version", None)
        if current_version != expected_version: raise Conflict("stale_version", "record version is stale")
        self.items[key] = value; self.s.fail(f"{self.name}_update"); return value
    def list(self): return tuple(self.items[key] for key in sorted(self.items))


class _Idempotency:
    def __init__(self, state): self.s = state
    def get(self, operation, tenant_id, actor_subject, key): return self.s.idempotency.get((operation, tenant_id, actor_subject, key))
    def put(self, record: OperationsIdempotencyRecord):
        scope = (record.operation, record.tenant_id, record.actor_subject, record.key); current = self.s.idempotency.get(scope)
        if current is not None:
            if current != record: raise Conflict("idempotency_conflict", "idempotency key has conflicting content")
            return current
        self.s.idempotency[scope] = record; self.s.fail("idempotency"); return record


class InMemoryOperationsUnitOfWork:
    def __init__(self, state):
        self.s = state
        for name in KEY_FIELDS: setattr(self, name, _Repository(state, name))
        self.idempotency = _Idempotency(state); self.outbox = state.outbox; self._committed = False
    def __enter__(self): self.s.lock.acquire(); self._snapshot = self.s.snapshot(); return self
    def commit(self): self._committed = True
    def rollback(self): self.s.restore(self._snapshot); self._committed = True
    def __exit__(self, *_):
        if not self._committed: self.s.restore(self._snapshot)
        self.s.lock.release()


class InMemoryOperationsUnitOfWorkFactory:
    def __init__(self, state): self.state = state
    def __call__(self): return InMemoryOperationsUnitOfWork(self.state)


class DeterministicTelemetryExporter:
    def __init__(self) -> None: self.samples = {}; self.health = {}; self.fail_next = False
    def _fail(self):
        if self.fail_next: self.fail_next = False; raise RuntimeError("deterministic exporter failure")
    def export_sample(self, sample): self._fail(); self.samples[sample.sample_id] = sample
    def export_health(self, snapshot): self._fail(); self.health[snapshot.snapshot_id] = snapshot
