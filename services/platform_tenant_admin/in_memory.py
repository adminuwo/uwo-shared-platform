"""Thread-safe rollback-capable tenant-administration repositories for tests."""

from __future__ import annotations

from threading import RLock

from services.data_service_common import Conflict, InMemoryOutbox, RepositoryIntegrityError

from .repositories import IdempotencyRecord, TenantPlan, WorkflowPage


class InMemoryTenantAdministrationState:
    def __init__(self) -> None:
        self.workflows = {}
        self.plans = {}
        self.steps = {}
        self.receipts = {}
        self.idempotency = {}
        self.outbox = InMemoryOutbox()
        self.lock = RLock()
        self.fail_next: str | None = None

    def fail(self, point: str) -> None:
        if self.fail_next == point:
            self.fail_next = None
            raise RepositoryIntegrityError("injected tenant-administration repository failure")

    def snapshot(self):
        return (
            dict(self.workflows), dict(self.plans), dict(self.steps), dict(self.receipts),
            dict(self.idempotency), self.outbox.snapshot(),
        )

    def restore(self, snapshot) -> None:
        self.workflows, self.plans, self.steps, self.receipts, self.idempotency, outbox = snapshot
        self.outbox.restore(outbox)


class _Workflows:
    def __init__(self, state): self.state = state
    def create(self, workflow, plan):
        if workflow.workflow_id in self.state.workflows: raise Conflict("workflow_exists", "workflow already exists")
        self.state.workflows[workflow.workflow_id] = workflow; self.state.plans[workflow.workflow_id] = plan; self.state.fail("workflow"); return workflow
    def get(self, workflow_id): return self.state.workflows.get(workflow_id)
    def get_plan(self, workflow_id): return self.state.plans.get(workflow_id)
    def update(self, workflow, expected_version):
        current = self.get(workflow.workflow_id)
        if current is None: raise Conflict("unknown_workflow", "workflow does not exist")
        if current.version != expected_version: raise Conflict("stale_version", "workflow version is stale")
        self.state.workflows[workflow.workflow_id] = workflow; self.state.fail("workflow_update"); return workflow
    def list(self, tenant_id, limit, cursor):
        values = sorted((item for item in self.state.workflows.values() if item.tenant_id == tenant_id), key=lambda item: item.workflow_id)
        start = int(cursor or 0); items = tuple(values[start:start + limit]); next_cursor = str(start + limit) if start + limit < len(values) else None
        return WorkflowPage(items, next_cursor)


class _Steps:
    def __init__(self, state): self.state = state
    def append(self, step):
        current = self.state.steps.get(step.step_id)
        if current is not None:
            if current != step: raise Conflict("workflow_step_conflict", "workflow step has conflicting content")
            return current
        self.state.steps[step.step_id] = step; self.state.fail("step"); return step
    def get(self, step_id): return self.state.steps.get(step_id)
    def update(self, step, expected_version):
        current = self.get(step.step_id)
        if current is None: raise Conflict("unknown_workflow_step", "workflow step does not exist")
        if current.version != expected_version: raise Conflict("stale_version", "workflow step version is stale")
        self.state.steps[step.step_id] = step; self.state.fail("step_update"); return step
    def list(self, workflow_id): return tuple(sorted((item for item in self.state.steps.values() if item.workflow_id == workflow_id), key=lambda item: item.step_id))


class _Receipts:
    def __init__(self, state): self.state = state
    def append(self, receipt):
        current = self.state.receipts.get(receipt.step_id)
        if current is not None:
            if current != receipt: raise Conflict("workflow_receipt_conflict", "workflow receipt has conflicting content")
            return current
        self.state.receipts[receipt.step_id] = receipt; self.state.fail("receipt"); return receipt
    def get_for_step(self, step_id): return self.state.receipts.get(step_id)
    def list(self, workflow_id): return tuple(sorted((item for item in self.state.receipts.values() if item.workflow_id == workflow_id), key=lambda item: item.step_id))


class _Idempotency:
    def __init__(self, state): self.state = state
    def get(self, operation, tenant_id, actor_subject, key): return self.state.idempotency.get((operation, tenant_id, actor_subject, key))
    def put(self, record: IdempotencyRecord):
        scope = (record.operation, record.tenant_id, record.actor_subject, record.key)
        current = self.state.idempotency.get(scope)
        if current is not None:
            if current != record: raise Conflict("idempotency_conflict", "idempotency key has conflicting content")
            return current
        self.state.idempotency[scope] = record; self.state.fail("idempotency"); return record


class InMemoryTenantAdministrationUnitOfWork:
    def __init__(self, state):
        self.state = state; self.workflows = _Workflows(state); self.steps = _Steps(state); self.receipts = _Receipts(state); self.idempotency = _Idempotency(state); self.outbox = state.outbox; self._committed = False
    def __enter__(self): self.state.lock.acquire(); self._snapshot = self.state.snapshot(); return self
    def commit(self): self._committed = True
    def rollback(self): self.state.restore(self._snapshot); self._committed = True
    def __exit__(self, *_):
        if not self._committed: self.state.restore(self._snapshot)
        self.state.lock.release()


class InMemoryTenantAdministrationUnitOfWorkFactory:
    def __init__(self, state): self.state = state
    def __call__(self): return InMemoryTenantAdministrationUnitOfWork(self.state)
