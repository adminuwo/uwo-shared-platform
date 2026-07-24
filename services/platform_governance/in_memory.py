"""Thread-safe rollback-capable governance test repositories."""

from __future__ import annotations

from threading import RLock

from services.data_service_common import Conflict, InMemoryOutbox, RepositoryIntegrityError

from .repositories import IdempotencyRecord


class InMemoryGovernanceState:
    def __init__(self) -> None:
        self.drafts = {}; self.validations = {}; self.changes = {}; self.approvals = {}; self.releases = {}
        self.promotions = {}; self.rollbacks = {}; self.idempotency = {}; self.outbox = InMemoryOutbox()
        self.lock = RLock(); self.fail_next: str | None = None

    def fail(self, point):
        if self.fail_next == point:
            self.fail_next = None; raise RepositoryIntegrityError("injected governance repository failure")

    def snapshot(self):
        return tuple(dict(item) for item in (self.drafts, self.validations, self.changes, self.approvals, self.releases, self.promotions, self.rollbacks, self.idempotency)) + (self.outbox.snapshot(),)

    def restore(self, snapshot):
        self.drafts, self.validations, self.changes, self.approvals, self.releases, self.promotions, self.rollbacks, self.idempotency, outbox = snapshot
        self.outbox.restore(outbox)


class _Drafts:
    def __init__(self, state): self.s = state
    def create(self, value):
        if value.draft_id in self.s.drafts: raise Conflict("policy_draft_exists", "policy draft already exists")
        self.s.drafts[value.draft_id] = value; self.s.fail("draft"); return value
    def get(self, key): return self.s.drafts.get(key)
    def update(self, value, expected_version):
        current = self.get(value.draft_id)
        if current is None: raise Conflict("unknown_policy_draft", "policy draft does not exist")
        if current.version != expected_version: raise Conflict("stale_version", "policy draft version is stale")
        self.s.drafts[value.draft_id] = value; self.s.fail("draft_update"); return value


class _Validations:
    def __init__(self, state): self.s = state
    def append(self, value):
        current = self.s.validations.get(value.validation_id)
        if current is not None:
            if current != value: raise Conflict("policy_validation_conflict", "policy validation has conflicting content")
            return current
        self.s.validations[value.validation_id] = value; return value
    def latest(self, draft_id):
        values = [value for value in self.s.validations.values() if value.draft_id == draft_id]
        return sorted(values, key=lambda item: (item.validated_at, item.validation_id))[-1] if values else None


class _Changes:
    def __init__(self, state): self.s = state
    def create(self, value):
        if value.change_request_id in self.s.changes: raise Conflict("policy_change_exists", "policy change request exists")
        self.s.changes[value.change_request_id] = value; return value
    def get(self, key): return self.s.changes.get(key)
    def update(self, value, expected_version):
        current = self.get(value.change_request_id)
        if current is None: raise Conflict("unknown_policy_change", "policy change request does not exist")
        if current.version != expected_version: raise Conflict("stale_version", "policy change version is stale")
        self.s.changes[value.change_request_id] = value; return value


class _Approvals:
    def __init__(self, state): self.s = state
    def append(self, value):
        current = next((item for item in self.s.approvals.values() if item.change_request_id == value.change_request_id and item.approver_subject == value.approver_subject), None)
        if current is not None:
            if current != value: raise Conflict("approval_conflict", "approver has already decided")
            return current
        self.s.approvals[value.approval_id] = value; return value
    def list(self, key): return tuple(sorted((item for item in self.s.approvals.values() if item.change_request_id == key), key=lambda item: item.approval_id))


class _Releases:
    def __init__(self, state): self.s = state
    def append(self, value):
        current = self.s.releases.get(value.release_id)
        if current is not None:
            if current != value: raise Conflict("policy_release_conflict", "policy release has conflicting content")
            return current
        self.s.releases[value.release_id] = value; self.s.fail("release"); return value
    def get(self, key): return self.s.releases.get(key)
    def list(self, tenant_id): return tuple(sorted((item for item in self.s.releases.values() if item.tenant_id == tenant_id), key=lambda item: item.created_at))


class _Promotions:
    def __init__(self, state): self.s = state
    def current(self, tenant_id, environment):
        values = [item for item in self.s.promotions.values() if item.tenant_id == tenant_id and item.environment is environment]
        return max(values, key=lambda item: item.environment_version) if values else None
    def append(self, value, expected_environment_version):
        current = self.current(value.tenant_id, value.environment); current_version = current.environment_version if current else 0
        if current_version != expected_environment_version: raise Conflict("stale_version", "policy environment version is stale")
        existing = self.s.promotions.get(value.promotion_id)
        if existing is not None:
            if existing != value: raise Conflict("policy_promotion_conflict", "promotion has conflicting content")
            return existing
        self.s.promotions[value.promotion_id] = value; self.s.fail("promotion"); return value
    def list(self, tenant_id): return tuple(sorted((item for item in self.s.promotions.values() if item.tenant_id == tenant_id), key=lambda item: (item.environment.value, item.environment_version)))
    def append_rollback(self, value):
        current = self.s.rollbacks.get(value.rollback_id)
        if current is not None:
            if current != value: raise Conflict("policy_rollback_conflict", "rollback has conflicting content")
            return current
        self.s.rollbacks[value.rollback_id] = value; return value
    def list_rollbacks(self, tenant_id): return tuple(sorted((item for item in self.s.rollbacks.values() if item.tenant_id == tenant_id), key=lambda item: item.created_at))


class _Idempotency:
    def __init__(self, state): self.s = state
    def get(self, operation, tenant_id, actor_subject, key): return self.s.idempotency.get((operation, tenant_id, actor_subject, key))
    def put(self, record: IdempotencyRecord):
        scope = (record.operation, record.tenant_id, record.actor_subject, record.key); current = self.s.idempotency.get(scope)
        if current is not None:
            if current != record: raise Conflict("idempotency_conflict", "idempotency key has conflicting content")
            return current
        self.s.idempotency[scope] = record; self.s.fail("idempotency"); return record


class InMemoryGovernanceUnitOfWork:
    def __init__(self, state):
        self.s = state; self.drafts = _Drafts(state); self.validations = _Validations(state); self.changes = _Changes(state); self.approvals = _Approvals(state); self.releases = _Releases(state); self.promotions = _Promotions(state); self.idempotency = _Idempotency(state); self.outbox = state.outbox; self._committed = False
    def __enter__(self): self.s.lock.acquire(); self._snapshot = self.s.snapshot(); return self
    def commit(self): self._committed = True
    def rollback(self): self.s.restore(self._snapshot); self._committed = True
    def __exit__(self, *_):
        if not self._committed: self.s.restore(self._snapshot)
        self.s.lock.release()


class InMemoryGovernanceUnitOfWorkFactory:
    def __init__(self, state): self.state = state
    def __call__(self): return InMemoryGovernanceUnitOfWork(self.state)
