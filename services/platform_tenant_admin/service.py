"""Restart-safe tenant-administration saga orchestration."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any, Callable, Mapping

from packages.contracts import (
    Permission, Product, TenantAdministrationStep, TenantAdministrationStepStatus,
    TenantAdministrationWorkflow, TenantAdministrationWorkflowStatus, TenantDecommissionPlan,
    TenantOnboardingPlan, TenantOperationalProfile, TenantReactivationPlan, TenantSuspensionPlan,
    VerifiedSubjectIdentity, WorkflowReceipt, operations_fingerprint, utc_now,
)
from services.data_service_common import (
    AuditSink, Conflict, DataServiceAuthorizer, InfrastructureUnavailable, InvalidRequest,
    OutboxRecord, OutboxStatus, PlatformEvent, ServiceAuditEvent, deterministic_id, platform_event,
)

from .repositories import (
    BillingAdministrationClient, ControlPlaneAdministrationClient, ExternalStepReceipt,
    GovernanceAdministrationClient, IdempotencyRecord, NotificationAdministrationClient,
    OperationsAdministrationClient, TenantPlan, UnitOfWorkFactory,
)

ONBOARDING_STEPS = (
    "validate-tenant-region", "baseline-membership-roles", "product-model-entitlements",
    "billing-account-readiness", "notification-preferences", "initial-policy-release",
    "audit-operations-registration",
)
SUSPENSION_STEPS = ("suspend-authoritative-tenant", "register-suspension-event")
REACTIVATION_STEPS = ("reactivate-authoritative-tenant", "register-reactivation-event")
TERMINAL = frozenset({TenantAdministrationWorkflowStatus.COMPLETED, TenantAdministrationWorkflowStatus.CANCELLED, TenantAdministrationWorkflowStatus.FAILED})


class StepExecutionFailure(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = True) -> None:
        super().__init__(code); self.code = code; self.retryable = retryable


class PlatformTenantAdministrationService:
    def __init__(
        self,
        uow: UnitOfWorkFactory,
        authorizer: DataServiceAuthorizer,
        audit: AuditSink,
        control_plane: ControlPlaneAdministrationClient,
        billing: BillingAdministrationClient,
        notifications: NotificationAdministrationClient,
        governance: GovernanceAdministrationClient,
        operations: OperationsAdministrationClient,
        *,
        clock: Callable[[], str] = utc_now,
        lease_seconds: int = 30,
    ) -> None:
        self._uow = uow; self._auth = authorizer; self._audit = audit; self._control_plane = control_plane
        self._billing = billing; self._notifications = notifications; self._governance = governance; self._operations = operations
        self._clock = clock; self._lease_seconds = lease_seconds

    @staticmethod
    def _require_key(key: str) -> None:
        if not isinstance(key, str) or not key.strip() or len(key) > 128:
            raise InvalidRequest("invalid_idempotency_key", "idempotency key must contain 1 to 128 characters")

    def _plan(self, plan_type, tenant_id: str, region: str, actor: str, operations: tuple[str, ...], key: str) -> TenantPlan:
        now = self._clock()
        return plan_type(deterministic_id("plan", plan_type.__name__, tenant_id, key), tenant_id, region, actor, operations, now)

    def start_onboarding(self, identity: VerifiedSubjectIdentity, tenant_id: str, region: str, metadata: Mapping[str, Any], idempotency_key: str, request_id: str):
        plan = self._plan(TenantOnboardingPlan, tenant_id, region, identity.subject, ONBOARDING_STEPS, idempotency_key)
        return self._start(identity, plan, metadata, idempotency_key, request_id)

    def start_suspension(self, identity, tenant_id, region, idempotency_key, request_id):
        plan = self._plan(TenantSuspensionPlan, tenant_id, region, identity.subject, SUSPENSION_STEPS, idempotency_key)
        return self._start(identity, plan, {}, idempotency_key, request_id)

    def start_reactivation(self, identity, tenant_id, region, idempotency_key, request_id):
        plan = self._plan(TenantReactivationPlan, tenant_id, region, identity.subject, REACTIVATION_STEPS, idempotency_key)
        return self._start(identity, plan, {}, idempotency_key, request_id)

    def create_decommission_plan(self, identity, tenant_id, region, request_id):
        self._auth.require(identity, tenant_id, Permission.TENANT_ADMIN_EXECUTE, allow_suspended=True)
        plan = self._plan(TenantDecommissionPlan, tenant_id, region, identity.subject, ("preserve-storage", "preserve-billing-ledger", "preserve-analytics", "preserve-audit", "preserve-notifications"), request_id)
        self._audit.emit(ServiceAuditEvent("tenant_admin.decommission_planned", request_id, "succeeded", tenant_id, identity.subject, resource_id=plan.plan_id))
        return plan

    def _start(self, identity, plan: TenantPlan, metadata: Mapping[str, Any], key: str, request_id: str):
        self._auth.require(identity, plan.tenant_id, Permission.TENANT_ADMIN_EXECUTE, allow_suspended=isinstance(plan, TenantReactivationPlan))
        self._require_key(key)
        fingerprint = operations_fingerprint({"plan": plan, "metadata": metadata})
        operation = f"start-{plan.__class__.__name__}"
        with self._uow() as tx:
            replay = tx.idempotency.get(operation, plan.tenant_id, identity.subject, key)
            if replay is not None:
                if replay.request_fingerprint != fingerprint: raise Conflict("idempotency_conflict", "idempotency key has conflicting content")
                tx.commit(); return replay.original_workflow
            workflow_id = deterministic_id("workflow", operation, plan.tenant_id, identity.subject, key)
            step_ids = tuple(deterministic_id("workflow-step", workflow_id, index, step) for index, step in enumerate(plan.step_operations, 1))
            now = self._clock()
            workflow = TenantAdministrationWorkflow(workflow_id, plan.tenant_id, operation, TenantAdministrationWorkflowStatus.PENDING, step_ids, 0, identity.subject, now, now, 1)
            tx.workflows.create(workflow, plan)
            for index, (step_id, step_operation) in enumerate(zip(step_ids, plan.step_operations), 1):
                tx.steps.append(TenantAdministrationStep(step_id, workflow_id, plan.tenant_id, step_operation, TenantAdministrationStepStatus.PENDING, deterministic_id("step-key", workflow_id, index), 0, now, now, 1))
            tx.idempotency.put(IdempotencyRecord(operation, plan.tenant_id, identity.subject, key, fingerprint, workflow))
            event = platform_event("tenant.workflow.started", plan.tenant_id, request_id, {"resource_id": workflow_id, "status": operation}, now)
            tx.outbox.enqueue(OutboxRecord(deterministic_id("outbox", event.event_id), event, OutboxStatus.PENDING, 0, now, 1))
            tx.commit()
        self._audit.emit(ServiceAuditEvent("tenant_admin.workflow_started", request_id, "succeeded", plan.tenant_id, identity.subject, resource_id=workflow_id))
        return workflow

    @staticmethod
    def _expired(step: TenantAdministrationStep, now: str) -> bool:
        if step.claim_expires_at is None: return False
        return datetime.fromisoformat(step.claim_expires_at.replace("Z", "+00:00")) <= datetime.fromisoformat(now.replace("Z", "+00:00"))

    def _execute(self, plan: TenantPlan, operation: str, key: str) -> ExternalStepReceipt:
        metadata = {"plan_id": plan.plan_id}
        if operation == "validate-tenant-region": return self._control_plane.validate_tenant(plan.tenant_id, plan.region, key)
        if operation == "baseline-membership-roles": return self._control_plane.ensure_baseline_membership(plan.tenant_id, metadata, key)
        if operation == "product-model-entitlements": return self._control_plane.ensure_entitlements(plan.tenant_id, metadata, key)
        if operation == "billing-account-readiness": return self._billing.ensure_account_ready(plan.tenant_id, key)
        if operation == "notification-preferences": return self._notifications.ensure_baseline_preferences(plan.tenant_id, key)
        if operation == "initial-policy-release": return self._governance.ensure_initial_policy_release(plan.tenant_id, key)
        if operation in {"audit-operations-registration", "register-suspension-event", "register-reactivation-event"}: return self._operations.register_tenant_operations(plan.tenant_id, key)
        if operation == "suspend-authoritative-tenant": return self._control_plane.set_tenant_status(plan.tenant_id, "suspended", key)
        if operation == "reactivate-authoritative-tenant": return self._control_plane.set_tenant_status(plan.tenant_id, "active", key)
        raise StepExecutionFailure("unknown_workflow_step", retryable=False)

    def continue_workflow(self, identity, tenant_id: str, workflow_id: str, expected_version: int, request_id: str, worker_id: str = "tenant-admin-worker"):
        self._auth.require(identity, tenant_id, Permission.TENANT_ADMIN_EXECUTE, allow_suspended=True)
        now = self._clock()
        with self._uow() as tx:
            workflow = tx.workflows.get(workflow_id)
            if workflow is None or workflow.tenant_id != tenant_id: raise Conflict("unknown_workflow", "workflow does not exist")
            if workflow.version != expected_version: raise Conflict("stale_version", "workflow version is stale")
            if workflow.status is TenantAdministrationWorkflowStatus.COMPLETED:
                tx.commit(); return workflow
            if workflow.status in {TenantAdministrationWorkflowStatus.CANCELLED, TenantAdministrationWorkflowStatus.FAILED}: raise Conflict("workflow_terminal", "terminal workflow cannot be resumed")
            if workflow.current_step >= len(workflow.step_ids): raise Conflict("workflow_integrity_error", "workflow has no executable step")
            step = tx.steps.get(workflow.step_ids[workflow.current_step])
            if step is None: raise Conflict("workflow_integrity_error", "workflow step does not exist")
            if step.status is TenantAdministrationStepStatus.CLAIMED and not self._expired(step, now): raise Conflict("workflow_step_claimed", "workflow step is already claimed")
            lease = (datetime.fromisoformat(now.replace("Z", "+00:00")) + timedelta(seconds=self._lease_seconds)).astimezone(timezone.utc).isoformat()
            claimed = tx.steps.update(replace(step, status=TenantAdministrationStepStatus.CLAIMED, claimed_by=worker_id, claim_expires_at=lease, attempt_count=step.attempt_count + 1, updated_at=now, version=step.version + 1, error_code=None), step.version)
            running = tx.workflows.update(replace(workflow, status=TenantAdministrationWorkflowStatus.RUNNING, updated_at=now, version=workflow.version + 1), workflow.version)
            plan = tx.workflows.get_plan(workflow_id)
            tx.commit()
        if plan is None: raise Conflict("workflow_integrity_error", "workflow plan does not exist")
        try:
            external = self._execute(plan, claimed.operation, claimed.idempotency_key)
        except StepExecutionFailure as exc:
            return self._fail_step(tenant_id, workflow_id, claimed.step_id, running.version, claimed.version, exc.code, exc.retryable, request_id)
        except Exception as exc:
            if isinstance(exc, (Conflict, InfrastructureUnavailable)):
                code = exc.code
            else:
                code = "dependency_unavailable"
            return self._fail_step(tenant_id, workflow_id, claimed.step_id, running.version, claimed.version, code, True, request_id)
        return self._complete_step(tenant_id, workflow_id, claimed, running.version, external, request_id)

    def _fail_step(self, tenant_id, workflow_id, step_id, workflow_version, step_version, code, retryable, request_id):
        now = self._clock()
        with self._uow() as tx:
            workflow = tx.workflows.get(workflow_id); step = tx.steps.get(step_id)
            if workflow is None or step is None: raise Conflict("workflow_integrity_error", "workflow state disappeared")
            if workflow.version != workflow_version or step.version != step_version or step.status is not TenantAdministrationStepStatus.CLAIMED:
                raise Conflict("stale_workflow_claim", "workflow claim is no longer current")
            step_status = TenantAdministrationStepStatus.BLOCKED if retryable else TenantAdministrationStepStatus.FAILED
            workflow_status = TenantAdministrationWorkflowStatus.BLOCKED if retryable else TenantAdministrationWorkflowStatus.FAILED
            tx.steps.update(replace(step, status=step_status, claimed_by=None, claim_expires_at=None, error_code=code, updated_at=now, version=step.version + 1), step.version)
            result = tx.workflows.update(replace(workflow, status=workflow_status, updated_at=now, version=workflow.version + 1), workflow.version)
            event = platform_event(f"tenant.workflow.{workflow_status.value}", tenant_id, request_id, {"resource_id": workflow_id, "reason_code": code}, now)
            tx.outbox.enqueue(OutboxRecord(deterministic_id("outbox", event.event_id), event, OutboxStatus.PENDING, 0, now, 1)); tx.commit()
        return result

    def _complete_step(self, tenant_id, workflow_id, claimed, workflow_version, external, request_id):
        now = self._clock()
        with self._uow() as tx:
            workflow = tx.workflows.get(workflow_id); step = tx.steps.get(claimed.step_id)
            if workflow is None or step is None: raise Conflict("workflow_integrity_error", "workflow state disappeared")
            if workflow.version != workflow_version or step.version != claimed.version or step.status is not TenantAdministrationStepStatus.CLAIMED:
                raise Conflict("stale_workflow_claim", "workflow claim is no longer current")
            existing = tx.receipts.get_for_step(step.step_id)
            receipt = WorkflowReceipt(deterministic_id("receipt", step.step_id), workflow_id, step.step_id, tenant_id, step.operation, external.result_reference, external.result_digest, now)
            if existing is None: tx.receipts.append(receipt)
            elif existing != receipt: raise Conflict("workflow_receipt_conflict", "workflow step has conflicting result")
            tx.steps.update(replace(step, status=TenantAdministrationStepStatus.COMPLETED, claimed_by=None, claim_expires_at=None, updated_at=now, version=step.version + 1), step.version)
            next_index = workflow.current_step + 1
            status = TenantAdministrationWorkflowStatus.COMPLETED if next_index == len(workflow.step_ids) else TenantAdministrationWorkflowStatus.PENDING
            result = tx.workflows.update(replace(workflow, current_step=next_index, status=status, updated_at=now, version=workflow.version + 1), workflow.version)
            if status is TenantAdministrationWorkflowStatus.COMPLETED:
                event = platform_event("tenant.workflow.completed", tenant_id, request_id, {"resource_id": workflow_id}, now)
                tx.outbox.enqueue(OutboxRecord(deterministic_id("outbox", event.event_id), event, OutboxStatus.PENDING, 0, now, 1))
            tx.commit()
        return result

    def cancel_workflow(self, identity, tenant_id, workflow_id, expected_version, request_id):
        self._auth.require(identity, tenant_id, Permission.TENANT_ADMIN_EXECUTE, allow_suspended=True)
        now = self._clock()
        with self._uow() as tx:
            workflow = tx.workflows.get(workflow_id)
            if workflow is None or workflow.tenant_id != tenant_id: raise Conflict("unknown_workflow", "workflow does not exist")
            if workflow.version != expected_version: raise Conflict("stale_version", "workflow version is stale")
            if workflow.status in TERMINAL: raise Conflict("workflow_terminal", "terminal workflow cannot be cancelled")
            step = tx.steps.get(workflow.step_ids[workflow.current_step]) if workflow.current_step < len(workflow.step_ids) else None
            if step is not None and step.status is TenantAdministrationStepStatus.CLAIMED and not self._expired(step, now):
                raise Conflict("workflow_step_claimed", "committed external mutation may still be in progress")
            result = tx.workflows.update(replace(workflow, status=TenantAdministrationWorkflowStatus.CANCELLED, cancellation_requested=True, updated_at=now, version=workflow.version + 1), workflow.version)
            event = platform_event("tenant.workflow.cancelled", tenant_id, request_id, {"resource_id": workflow_id}, now)
            tx.outbox.enqueue(OutboxRecord(deterministic_id("outbox", event.event_id), event, OutboxStatus.PENDING, 0, now, 1)); tx.commit(); return result

    def get_workflow(self, identity, tenant_id, workflow_id):
        self._auth.require(identity, tenant_id, Permission.TENANT_ADMIN_READ, allow_suspended=True)
        with self._uow() as tx: workflow = tx.workflows.get(workflow_id); tx.commit()
        if workflow is None or workflow.tenant_id != tenant_id: raise Conflict("unknown_workflow", "workflow does not exist")
        return workflow

    def list_workflows(self, identity, tenant_id, limit=50, cursor=None):
        self._auth.require(identity, tenant_id, Permission.TENANT_ADMIN_READ, allow_suspended=True)
        with self._uow() as tx: result = tx.workflows.list(tenant_id, limit, cursor); tx.commit(); return result

    def read_operational_profile(self, identity, tenant_id):
        self._auth.require(identity, tenant_id, Permission.TENANT_ADMIN_READ, allow_suspended=True)
        tenant = self._control_plane.tenant_profile(tenant_id); billing = self._billing.billing_profile(tenant_id)
        return TenantOperationalProfile(tenant_id, tenant["region"], tenant["status"], billing["status"], self._governance.active_release_id(tenant_id), tenant["status"] == "active", tenant["status"] == "active", self._clock())
