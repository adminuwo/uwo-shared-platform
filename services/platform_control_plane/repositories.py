"""Persistence boundaries for control-plane state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Union

from packages.contracts import ModelEntitlement, PolicyDocument, Product, ProductEntitlement, Role, Tenant, TenantMembership


@dataclass(frozen=True)
class Page:
    items: tuple[Tenant, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class CreateResult:
    tenant: Tenant
    created: bool


@dataclass(frozen=True)
class EntitlementMutationResult:
    entitlement: ProductEntitlement | ModelEntitlement
    created: bool


@dataclass(frozen=True)
class EntitlementSnapshot:
    tenant_id: str
    products: tuple[ProductEntitlement, ...]
    models: tuple[ModelEntitlement, ...]
    version: int


@dataclass(frozen=True)
class IdempotencyScope:
    operation: str
    tenant_id: str
    actor_subject: str

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value for value in (self.operation, self.tenant_id, self.actor_subject)):
            raise ValueError("idempotency scope fields must be non-empty strings")


IdempotencyResult = Union[Tenant, ProductEntitlement, ModelEntitlement]


@dataclass(frozen=True)
class IdempotencyRecord:
    scope: IdempotencyScope
    key: str
    request_fingerprint: str
    original_result: IdempotencyResult

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key or not isinstance(self.request_fingerprint, str) or not self.request_fingerprint:
            raise ValueError("idempotency record key and fingerprint must be non-empty strings")
        if not isinstance(self.original_result, (Tenant, ProductEntitlement, ModelEntitlement)):
            raise ValueError("idempotency record must contain an immutable operation-result snapshot")


class TenantRepository(Protocol):
    def create(self, tenant: Tenant) -> Tenant: ...
    def get(self, tenant_id: str) -> Tenant | None: ...
    def update(self, tenant: Tenant, expected_version: int) -> Tenant: ...
    def list(self, limit: int, cursor: str | None) -> Page: ...


class MembershipRepository(Protocol):
    def get(self, tenant_id: str, subject: str) -> TenantMembership | None: ...
    def create(self, membership: TenantMembership) -> TenantMembership: ...
    def update(self, membership: TenantMembership, expected_version: int) -> TenantMembership: ...


class RoleRepository(Protocol):
    def get(self, role_id: str) -> Role | None: ...
    def list(self) -> tuple[Role, ...]: ...


class EntitlementRepository(Protocol):
    def initialize(self, tenant_id: str) -> None: ...
    def snapshot(self, tenant_id: str) -> EntitlementSnapshot: ...
    def grant_product(self, entitlement: ProductEntitlement, expected_version: int) -> ProductEntitlement: ...
    def revoke_product(self, tenant_id: str, product: Product, expected_version: int) -> EntitlementSnapshot: ...
    def grant_model(self, entitlement: ModelEntitlement, expected_version: int) -> ModelEntitlement: ...
    def revoke_model(self, tenant_id: str, model: str, expected_version: int) -> EntitlementSnapshot: ...


class PolicyVersionRepository(Protocol):
    def create_initial(self, document: PolicyDocument) -> PolicyDocument: ...
    def current(self, tenant_id: str) -> PolicyDocument | None: ...


class IdempotencyRepository(Protocol):
    def get(self, scope: IdempotencyScope, key: str) -> IdempotencyRecord | None: ...
    def put(self, record: IdempotencyRecord) -> IdempotencyRecord: ...


class ControlPlaneUnitOfWork(Protocol):
    tenants: TenantRepository
    memberships: MembershipRepository
    roles: RoleRepository
    entitlements: EntitlementRepository
    policies: PolicyVersionRepository
    idempotency: IdempotencyRepository
    outbox: object

    def __enter__(self) -> "ControlPlaneUnitOfWork": ...
    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> ControlPlaneUnitOfWork: ...
