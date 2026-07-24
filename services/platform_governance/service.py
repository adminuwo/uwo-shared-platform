"""Immutable policy draft, approval, release, promotion, and rollback service."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from typing import Callable, Mapping, Any

from packages.contracts import (
    ApprovalDecision, ConfigurationDigest, HIGH_RISK_CATEGORIES, Permission, PolicyApproval,
    PolicyChangeRequest, PolicyDraft, PolicyDraftStatus, PolicyEnvironment, PolicyPromotion,
    PolicyRelease, PolicyRollback, PolicyValidationResult, VerifiedSubjectIdentity,
    operations_fingerprint, operations_json, utc_now,
)
from services.data_service_common import (
    AuditSink, Conflict, DataServiceAuthorizer, InvalidRequest, OutboxRecord, OutboxStatus,
    PolicyViolation, ResourceNotFound, ServiceAuditEvent, deterministic_id, platform_event,
)
from .repositories import IdempotencyRecord, PolicyHistory, UnitOfWorkFactory


class PlatformGovernanceService:
    def __init__(self, uow: UnitOfWorkFactory, authorizer: DataServiceAuthorizer, audit: AuditSink, *, compatibility_version: str = "uwo-policy-v1", production_approvals: int = 2, clock: Callable[[], str] = utc_now) -> None:
        self._uow = uow; self._auth = authorizer; self._audit = audit; self._compatibility = compatibility_version; self._production_approvals = max(2, production_approvals); self._clock = clock

    def create_draft(self, identity, tenant_id, content, compatibility_version, base_release_id, risk_categories, request_id):
        self._auth.require(identity, tenant_id, Permission.GOVERNANCE_DRAFT)
        now = self._clock(); draft_id = deterministic_id("policy-draft", tenant_id, identity.subject, request_id)
        draft = PolicyDraft(draft_id, tenant_id, identity.subject, PolicyDraftStatus.DRAFT, compatibility_version, content, base_release_id, tuple(sorted(set(risk_categories))), now, now, 1)
        with self._uow() as tx: result = tx.drafts.create(draft); tx.commit()
        self._audit.emit(ServiceAuditEvent("governance.draft_created", request_id, "succeeded", tenant_id, identity.subject, resource_id=draft_id)); return result

    def update_draft(self, identity, tenant_id, draft_id, content, risk_categories, expected_version, request_id):
        self._auth.require(identity, tenant_id, Permission.GOVERNANCE_DRAFT)
        with self._uow() as tx:
            draft = tx.drafts.get(draft_id)
            if draft is None or draft.tenant_id != tenant_id: raise ResourceNotFound("unknown_policy_draft", "policy draft does not exist")
            if draft.status not in {PolicyDraftStatus.DRAFT, PolicyDraftStatus.VALIDATED}: raise PolicyViolation("policy_draft_immutable", "submitted policy draft cannot be updated")
            result = tx.drafts.update(replace(draft, content=content, risk_categories=tuple(sorted(set(risk_categories))), status=PolicyDraftStatus.DRAFT, updated_at=self._clock(), version=draft.version + 1), expected_version); tx.commit(); return result

    def validate_draft(self, identity, tenant_id, draft_id, request_id):
        self._auth.require(identity, tenant_id, Permission.GOVERNANCE_DRAFT)
        now = self._clock()
        with self._uow() as tx:
            draft = tx.drafts.get(draft_id)
            if draft is None or draft.tenant_id != tenant_id: raise ResourceNotFound("unknown_policy_draft", "policy draft does not exist")
            errors = () if draft.compatibility_version == self._compatibility else ("incompatible-policy-version",)
            validation = PolicyValidationResult(deterministic_id("policy-validation", draft_id, draft.version), draft_id, tenant_id, not errors, draft.compatibility_version, errors, now)
            tx.validations.append(validation)
            if validation.valid and draft.status is PolicyDraftStatus.DRAFT:
                tx.drafts.update(replace(draft, status=PolicyDraftStatus.VALIDATED, updated_at=now, version=draft.version + 1), draft.version)
                event = platform_event("policy.validated", tenant_id, request_id, {"resource_id": validation.validation_id}, now)
                tx.outbox.enqueue(OutboxRecord(deterministic_id("outbox", event.event_id), event, OutboxStatus.PENDING, 0, now, 1))
            tx.commit()
        return validation

    def submit_change(self, identity, tenant_id, draft_id, request_id):
        self._auth.require(identity, tenant_id, Permission.GOVERNANCE_DRAFT)
        now = self._clock()
        with self._uow() as tx:
            draft = tx.drafts.get(draft_id); validation = tx.validations.latest(draft_id)
            if draft is None or draft.tenant_id != tenant_id: raise ResourceNotFound("unknown_policy_draft", "policy draft does not exist")
            if validation is None or not validation.valid or draft.status is not PolicyDraftStatus.VALIDATED: raise PolicyViolation("policy_not_validated", "only a validated policy draft may be submitted")
            required = 2 if set(draft.risk_categories) & HIGH_RISK_CATEGORIES else 1
            change = PolicyChangeRequest(deterministic_id("policy-change", draft_id), draft_id, tenant_id, draft.proposer_subject, validation.validation_id, PolicyDraftStatus.SUBMITTED, required, now, 1)
            tx.changes.create(change); tx.drafts.update(replace(draft, status=PolicyDraftStatus.SUBMITTED, updated_at=now, version=draft.version + 1), draft.version); tx.commit(); return change

    def decide_change(self, identity, tenant_id, change_request_id, decision, reason_code, request_id):
        self._auth.require(identity, tenant_id, Permission.GOVERNANCE_APPROVE)
        now = self._clock()
        with self._uow() as tx:
            change = tx.changes.get(change_request_id)
            if change is None or change.tenant_id != tenant_id: raise ResourceNotFound("unknown_policy_change", "policy change request does not exist")
            if identity.subject == change.proposer_subject: raise PolicyViolation("self_approval_denied", "policy proposer cannot approve their own change")
            if change.status not in {PolicyDraftStatus.SUBMITTED, PolicyDraftStatus.RELEASED} or (change.status is PolicyDraftStatus.RELEASED and ApprovalDecision(decision) is ApprovalDecision.REJECTED):
                raise PolicyViolation("policy_change_terminal", "policy change is no longer open for this decision")
            approval = PolicyApproval(deterministic_id("policy-approval", change_request_id, identity.subject), change_request_id, tenant_id, identity.subject, ApprovalDecision(decision), reason_code, now)
            tx.approvals.append(approval)
            if approval.decision is ApprovalDecision.REJECTED:
                tx.changes.update(replace(change, status=PolicyDraftStatus.REJECTED, version=change.version + 1), change.version)
                draft = tx.drafts.get(change.draft_id)
                tx.drafts.update(replace(draft, status=PolicyDraftStatus.REJECTED, updated_at=now, version=draft.version + 1), draft.version)
            event = platform_event(f"policy.{approval.decision.value}", tenant_id, request_id, {"resource_id": change_request_id, "reason_code": reason_code}, now)
            tx.outbox.enqueue(OutboxRecord(deterministic_id("outbox", event.event_id), event, OutboxStatus.PENDING, 0, now, 1)); tx.commit(); return approval

    def create_release(self, identity, tenant_id, change_request_id, request_id):
        self._auth.require(identity, tenant_id, Permission.GOVERNANCE_DRAFT)
        now = self._clock()
        with self._uow() as tx:
            change = tx.changes.get(change_request_id)
            if change is None or change.tenant_id != tenant_id: raise ResourceNotFound("unknown_policy_change", "policy change request does not exist")
            if change.status is PolicyDraftStatus.REJECTED: raise PolicyViolation("policy_change_rejected", "rejected policy change cannot be released")
            approvals = tuple(item for item in tx.approvals.list(change_request_id) if item.decision is ApprovalDecision.APPROVED)
            if len({item.approver_subject for item in approvals}) < change.required_approvals: raise PolicyViolation("policy_approval_incomplete", "required distinct approvals are missing")
            draft = tx.drafts.get(change.draft_id)
            digest = ConfigurationDigest("sha256", hashlib.sha256(operations_json(draft.content).encode()).hexdigest())
            release = PolicyRelease(deterministic_id("policy-release", tenant_id, change_request_id), tenant_id, change_request_id, draft.compatibility_version, draft.content, digest, draft.base_release_id, identity.subject, now)
            tx.releases.append(release); tx.changes.update(replace(change, status=PolicyDraftStatus.RELEASED, version=change.version + 1), change.version); tx.drafts.update(replace(draft, status=PolicyDraftStatus.RELEASED, updated_at=now, version=draft.version + 1), draft.version); tx.commit(); return release

    def promote(self, identity, tenant_id, release_id, environment, expected_environment_version, idempotency_key, request_id):
        environment = PolicyEnvironment(environment)
        if environment is PolicyEnvironment.PRODUCTION: self._auth.require_platform_admin(identity)
        else: self._auth.require(identity, tenant_id, Permission.GOVERNANCE_PROMOTE)
        if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 128: raise InvalidRequest("invalid_idempotency_key", "idempotency key is required")
        operation = f"promote-{environment.value}"; fingerprint = operations_fingerprint({"release_id": release_id, "environment": environment, "expected_version": expected_environment_version})
        now = self._clock()
        with self._uow() as tx:
            replay = tx.idempotency.get(operation, tenant_id, identity.subject, idempotency_key)
            if replay is not None:
                if replay.request_fingerprint != fingerprint: raise Conflict("idempotency_conflict", "idempotency key has conflicting content")
                tx.commit(); return replay.original_result
            release = tx.releases.get(release_id)
            if release is None or release.tenant_id != tenant_id: raise ResourceNotFound("unknown_policy_release", "policy release does not exist")
            current = tx.promotions.current(tenant_id, environment); current_id = current.release_id if current else None
            if release.source_release_id != current_id: raise Conflict("stale_base_release", "release base does not match active environment release")
            change = tx.changes.get(release.change_request_id); approvals = tx.approvals.list(release.change_request_id)
            distinct = {item.approver_subject for item in approvals if item.decision is ApprovalDecision.APPROVED}
            required = self._production_approvals if environment is PolicyEnvironment.PRODUCTION else change.required_approvals
            if any(item.decision is ApprovalDecision.REJECTED for item in approvals) or len(distinct) < required: raise PolicyViolation("policy_approval_incomplete", "promotion lacks required distinct approvals")
            promotion = PolicyPromotion(deterministic_id("policy-promotion", tenant_id, environment, release_id, expected_environment_version + 1), tenant_id, release_id, environment, identity.subject, now, expected_environment_version + 1, current_id)
            tx.promotions.append(promotion, expected_environment_version)
            tx.idempotency.put(IdempotencyRecord(operation, tenant_id, identity.subject, idempotency_key, fingerprint, promotion))
            event = platform_event("policy.promoted", tenant_id, request_id, {"resource_id": promotion.promotion_id, "status": environment.value}, now)
            tx.outbox.enqueue(OutboxRecord(deterministic_id("outbox", event.event_id), event, OutboxStatus.PENDING, 0, now, 1)); tx.commit()
        self._audit.emit(ServiceAuditEvent("governance.policy_promoted", request_id, "succeeded", tenant_id, identity.subject, resource_id=promotion.promotion_id)); return promotion

    def rollback(self, identity, tenant_id, environment, target_release_id, expected_environment_version, idempotency_key, request_id):
        environment = PolicyEnvironment(environment)
        if environment is PolicyEnvironment.PRODUCTION: self._auth.require_platform_admin(identity)
        else: self._auth.require(identity, tenant_id, Permission.GOVERNANCE_PROMOTE)
        operation = f"rollback-{environment.value}"; fingerprint = operations_fingerprint({"target": target_release_id, "expected_version": expected_environment_version}); now = self._clock()
        with self._uow() as tx:
            replay = tx.idempotency.get(operation, tenant_id, identity.subject, idempotency_key)
            if replay is not None:
                if replay.request_fingerprint != fingerprint: raise Conflict("idempotency_conflict", "idempotency key has conflicting content")
                tx.commit(); return replay.original_result
            current = tx.promotions.current(tenant_id, environment); target = tx.releases.get(target_release_id)
            if current is None or target is None or target.tenant_id != tenant_id: raise ResourceNotFound("unknown_policy_release", "rollback release does not exist")
            digest = ConfigurationDigest("sha256", hashlib.sha256(operations_json(target.content).encode()).hexdigest())
            release = PolicyRelease(deterministic_id("policy-release", "rollback", tenant_id, environment, target_release_id, expected_environment_version), tenant_id, target.change_request_id, target.compatibility_version, target.content, digest, current.release_id, identity.subject, now)
            tx.releases.append(release)
            promotion = PolicyPromotion(deterministic_id("policy-promotion", "rollback", tenant_id, environment, expected_environment_version + 1), tenant_id, release.release_id, environment, identity.subject, now, expected_environment_version + 1, current.release_id)
            tx.promotions.append(promotion, expected_environment_version)
            result = PolicyRollback(deterministic_id("policy-rollback", promotion.promotion_id), tenant_id, environment, current.release_id, target_release_id, promotion.promotion_id, identity.subject, now)
            tx.promotions.append_rollback(result); tx.idempotency.put(IdempotencyRecord(operation, tenant_id, identity.subject, idempotency_key, fingerprint, result))
            event = platform_event("policy.rolled_back", tenant_id, request_id, {"resource_id": result.rollback_id}, now)
            tx.outbox.enqueue(OutboxRecord(deterministic_id("outbox", event.event_id), event, OutboxStatus.PENDING, 0, now, 1)); tx.commit(); return result

    def active_release(self, identity, tenant_id, environment):
        self._auth.require(identity, tenant_id, Permission.GOVERNANCE_READ, allow_suspended=True)
        with self._uow() as tx:
            promotion = tx.promotions.current(tenant_id, PolicyEnvironment(environment)); release = tx.releases.get(promotion.release_id) if promotion else None; tx.commit()
        if release is None: raise ResourceNotFound("unknown_active_policy", "environment has no active policy release")
        return release

    def compare_releases(self, identity, tenant_id, left_id, right_id):
        self._auth.require(identity, tenant_id, Permission.GOVERNANCE_READ, allow_suspended=True)
        with self._uow() as tx: left = tx.releases.get(left_id); right = tx.releases.get(right_id); tx.commit()
        if left is None or right is None or left.tenant_id != tenant_id or right.tenant_id != tenant_id: raise ResourceNotFound("unknown_policy_release", "policy release does not exist")
        return {"left_release_id": left_id, "right_release_id": right_id, "same_digest": left.digest == right.digest, "left_digest": left.digest.digest, "right_digest": right.digest.digest}

    def history(self, identity, tenant_id):
        self._auth.require(identity, tenant_id, Permission.GOVERNANCE_READ, allow_suspended=True)
        with self._uow() as tx: result = PolicyHistory(tx.releases.list(tenant_id), tx.promotions.list(tenant_id), tx.promotions.list_rollbacks(tenant_id)); tx.commit(); return result
