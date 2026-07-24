"""Policy-governance repository and UnitOfWork protocols."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from packages.contracts import (
    PolicyApproval, PolicyChangeRequest, PolicyDraft, PolicyEnvironment, PolicyPromotion,
    PolicyRelease, PolicyRollback, PolicyValidationResult,
)


@dataclass(frozen=True)
class PolicyHistory:
    releases: tuple[PolicyRelease, ...]
    promotions: tuple[PolicyPromotion, ...]
    rollbacks: tuple[PolicyRollback, ...]


@dataclass(frozen=True)
class IdempotencyRecord:
    operation: str
    tenant_id: str
    actor_subject: str
    key: str
    request_fingerprint: str
    original_result: PolicyPromotion | PolicyRollback


class DraftRepository(Protocol):
    def create(self, draft: PolicyDraft) -> PolicyDraft: ...
    def get(self, draft_id: str) -> PolicyDraft | None: ...
    def update(self, draft: PolicyDraft, expected_version: int) -> PolicyDraft: ...


class ValidationRepository(Protocol):
    def append(self, validation: PolicyValidationResult) -> PolicyValidationResult: ...
    def latest(self, draft_id: str) -> PolicyValidationResult | None: ...


class ChangeRequestRepository(Protocol):
    def create(self, change: PolicyChangeRequest) -> PolicyChangeRequest: ...
    def get(self, change_request_id: str) -> PolicyChangeRequest | None: ...
    def update(self, change: PolicyChangeRequest, expected_version: int) -> PolicyChangeRequest: ...


class ApprovalRepository(Protocol):
    def append(self, approval: PolicyApproval) -> PolicyApproval: ...
    def list(self, change_request_id: str) -> tuple[PolicyApproval, ...]: ...


class ReleaseRepository(Protocol):
    def append(self, release: PolicyRelease) -> PolicyRelease: ...
    def get(self, release_id: str) -> PolicyRelease | None: ...
    def list(self, tenant_id: str) -> tuple[PolicyRelease, ...]: ...


class PromotionRepository(Protocol):
    def current(self, tenant_id: str, environment: PolicyEnvironment) -> PolicyPromotion | None: ...
    def append(self, promotion: PolicyPromotion, expected_environment_version: int) -> PolicyPromotion: ...
    def list(self, tenant_id: str) -> tuple[PolicyPromotion, ...]: ...
    def append_rollback(self, rollback: PolicyRollback) -> PolicyRollback: ...
    def list_rollbacks(self, tenant_id: str) -> tuple[PolicyRollback, ...]: ...


class IdempotencyRepository(Protocol):
    def get(self, operation: str, tenant_id: str, actor_subject: str, key: str) -> IdempotencyRecord | None: ...
    def put(self, record: IdempotencyRecord) -> IdempotencyRecord: ...


class GovernanceUnitOfWork(Protocol):
    drafts: DraftRepository
    validations: ValidationRepository
    changes: ChangeRequestRepository
    approvals: ApprovalRepository
    releases: ReleaseRepository
    promotions: PromotionRepository
    idempotency: IdempotencyRepository
    outbox: object

    def __enter__(self) -> "GovernanceUnitOfWork": ...
    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> GovernanceUnitOfWork: ...
