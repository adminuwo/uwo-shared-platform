"""Provider-neutral saga clients and persistence protocols."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Union

from packages.contracts import (
    TenantAdministrationStep, TenantAdministrationWorkflow, TenantDecommissionPlan,
    TenantOnboardingPlan, TenantReactivationPlan, TenantSuspensionPlan, WorkflowReceipt,
)

TenantPlan = Union[TenantOnboardingPlan, TenantSuspensionPlan, TenantReactivationPlan, TenantDecommissionPlan]


@dataclass(frozen=True)
class ExternalStepReceipt:
    result_reference: str
    result_digest: str


@dataclass(frozen=True)
class WorkflowPage:
    items: tuple[TenantAdministrationWorkflow, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class IdempotencyRecord:
    operation: str
    tenant_id: str
    actor_subject: str
    key: str
    request_fingerprint: str
    original_workflow: TenantAdministrationWorkflow


class WorkflowRepository(Protocol):
    def create(self, workflow: TenantAdministrationWorkflow, plan: TenantPlan) -> TenantAdministrationWorkflow: ...
    def get(self, workflow_id: str) -> TenantAdministrationWorkflow | None: ...
    def get_plan(self, workflow_id: str) -> TenantPlan | None: ...
    def update(self, workflow: TenantAdministrationWorkflow, expected_version: int) -> TenantAdministrationWorkflow: ...
    def list(self, tenant_id: str, limit: int, cursor: str | None) -> WorkflowPage: ...


class WorkflowStepRepository(Protocol):
    def append(self, step: TenantAdministrationStep) -> TenantAdministrationStep: ...
    def get(self, step_id: str) -> TenantAdministrationStep | None: ...
    def update(self, step: TenantAdministrationStep, expected_version: int) -> TenantAdministrationStep: ...
    def list(self, workflow_id: str) -> tuple[TenantAdministrationStep, ...]: ...


class WorkflowReceiptRepository(Protocol):
    def append(self, receipt: WorkflowReceipt) -> WorkflowReceipt: ...
    def get_for_step(self, step_id: str) -> WorkflowReceipt | None: ...
    def list(self, workflow_id: str) -> tuple[WorkflowReceipt, ...]: ...


class IdempotencyRepository(Protocol):
    def get(self, operation: str, tenant_id: str, actor_subject: str, key: str) -> IdempotencyRecord | None: ...
    def put(self, record: IdempotencyRecord) -> IdempotencyRecord: ...


class TenantAdministrationUnitOfWork(Protocol):
    workflows: WorkflowRepository
    steps: WorkflowStepRepository
    receipts: WorkflowReceiptRepository
    idempotency: IdempotencyRepository
    outbox: object

    def __enter__(self) -> "TenantAdministrationUnitOfWork": ...
    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> TenantAdministrationUnitOfWork: ...


class ControlPlaneAdministrationClient(Protocol):
    def validate_tenant(self, tenant_id: str, region: str, idempotency_key: str) -> ExternalStepReceipt: ...
    def ensure_baseline_membership(self, tenant_id: str, metadata: Mapping[str, Any], idempotency_key: str) -> ExternalStepReceipt: ...
    def ensure_entitlements(self, tenant_id: str, metadata: Mapping[str, Any], idempotency_key: str) -> ExternalStepReceipt: ...
    def set_tenant_status(self, tenant_id: str, status: str, idempotency_key: str) -> ExternalStepReceipt: ...
    def tenant_profile(self, tenant_id: str) -> Mapping[str, Any]: ...


class BillingAdministrationClient(Protocol):
    def ensure_account_ready(self, tenant_id: str, idempotency_key: str) -> ExternalStepReceipt: ...
    def billing_profile(self, tenant_id: str) -> Mapping[str, Any]: ...


class NotificationAdministrationClient(Protocol):
    def ensure_baseline_preferences(self, tenant_id: str, idempotency_key: str) -> ExternalStepReceipt: ...


class GovernanceAdministrationClient(Protocol):
    def ensure_initial_policy_release(self, tenant_id: str, idempotency_key: str) -> ExternalStepReceipt: ...
    def active_release_id(self, tenant_id: str) -> str: ...


class OperationsAdministrationClient(Protocol):
    def register_tenant_operations(self, tenant_id: str, idempotency_key: str) -> ExternalStepReceipt: ...
