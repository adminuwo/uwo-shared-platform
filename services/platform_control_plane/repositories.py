"""Persistence boundaries for control-plane state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

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


class TenantRepository(Protocol):
    def create(self, tenant: Tenant, idempotency_key: str, fingerprint: str) -> CreateResult: ...
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
    def grant_product(self, entitlement: ProductEntitlement, expected_version: int, idempotency_key: str, fingerprint: str) -> EntitlementMutationResult: ...
    def revoke_product(self, tenant_id: str, product: Product, expected_version: int) -> EntitlementSnapshot: ...
    def grant_model(self, entitlement: ModelEntitlement, expected_version: int, idempotency_key: str, fingerprint: str) -> EntitlementMutationResult: ...
    def revoke_model(self, tenant_id: str, model: str, expected_version: int) -> EntitlementSnapshot: ...


class PolicyVersionRepository(Protocol):
    def create_initial(self, document: PolicyDocument) -> PolicyDocument: ...
    def current(self, tenant_id: str) -> PolicyDocument | None: ...
