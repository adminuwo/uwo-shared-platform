"""Thread-safe in-memory repository implementations for tests only."""

from __future__ import annotations

from dataclasses import replace
from threading import RLock

from packages.contracts import ModelEntitlement, PolicyDocument, Product, ProductEntitlement, Role, Tenant, TenantMembership

from .errors import Conflict, ResourceNotFound, StaleVersion
from .repositories import CreateResult, EntitlementMutationResult, EntitlementSnapshot, Page


class InMemoryTenantRepository:
    """Test-only tenant repository; production must inject durable storage."""

    def __init__(self) -> None:
        self._items: dict[str, Tenant] = {}
        self._idempotency: dict[str, tuple[str, str]] = {}
        self._lock = RLock()

    def create(self, tenant: Tenant, idempotency_key: str, fingerprint: str) -> CreateResult:
        with self._lock:
            prior = self._idempotency.get(idempotency_key)
            if prior:
                prior_fingerprint, tenant_id = prior
                if prior_fingerprint != fingerprint:
                    raise Conflict("idempotency_conflict", "idempotency key was already used for a different tenant creation")
                return CreateResult(self._items[tenant_id], False)
            if tenant.tenant_id in self._items:
                raise Conflict("tenant_exists", "tenant already exists")
            self._items[tenant.tenant_id] = tenant
            self._idempotency[idempotency_key] = (fingerprint, tenant.tenant_id)
            return CreateResult(tenant, True)

    def get(self, tenant_id: str) -> Tenant | None:
        with self._lock:
            return self._items.get(tenant_id)

    def update(self, tenant: Tenant, expected_version: int) -> Tenant:
        with self._lock:
            current = self._items.get(tenant.tenant_id)
            if current is None:
                raise ResourceNotFound("unknown_tenant", "tenant does not exist")
            if current.version != expected_version:
                raise StaleVersion("stale_version", "tenant version is stale")
            self._items[tenant.tenant_id] = tenant
            return tenant

    def list(self, limit: int, cursor: str | None) -> Page:
        with self._lock:
            ids = sorted(self._items)
            if cursor is not None and cursor not in self._items:
                raise Conflict("invalid_cursor", "pagination cursor is invalid")
            start = ids.index(cursor) + 1 if cursor is not None else 0
            selected = ids[start:start + limit]
            next_cursor = selected[-1] if start + limit < len(ids) and selected else None
            return Page(tuple(self._items[item] for item in selected), next_cursor)


class InMemoryMembershipRepository:
    """Test-only membership repository."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], TenantMembership] = {}
        self._lock = RLock()

    def get(self, tenant_id: str, subject: str) -> TenantMembership | None:
        with self._lock:
            return self._items.get((tenant_id, subject))

    def create(self, membership: TenantMembership) -> TenantMembership:
        with self._lock:
            key = (membership.tenant_id, membership.subject)
            if key in self._items:
                raise Conflict("membership_exists", "membership already exists")
            self._items[key] = membership
            return membership

    def update(self, membership: TenantMembership, expected_version: int) -> TenantMembership:
        with self._lock:
            key = (membership.tenant_id, membership.subject)
            current = self._items.get(key)
            if current is None:
                raise ResourceNotFound("unknown_membership", "membership does not exist")
            if current.version != expected_version:
                raise StaleVersion("stale_version", "membership version is stale")
            self._items[key] = membership
            return membership


class InMemoryRoleRepository:
    """Test-only immutable role catalog."""

    def __init__(self, roles: tuple[Role, ...]) -> None:
        self._items = {role.role_id: role for role in roles}
        if len(self._items) != len(roles):
            raise ValueError("role identifiers must be unique")

    def get(self, role_id: str) -> Role | None:
        return self._items.get(role_id)

    def list(self) -> tuple[Role, ...]:
        return tuple(self._items[key] for key in sorted(self._items))


class InMemoryEntitlementRepository:
    """Test-only tenant entitlement aggregate with optimistic versions."""

    def __init__(self) -> None:
        self._products: dict[str, dict[Product, ProductEntitlement]] = {}
        self._models: dict[str, dict[str, ModelEntitlement]] = {}
        self._versions: dict[str, int] = {}
        self._idempotency: dict[str, tuple[str, str, str]] = {}
        self._lock = RLock()

    def initialize(self, tenant_id: str) -> None:
        with self._lock:
            self._products.setdefault(tenant_id, {})
            self._models.setdefault(tenant_id, {})
            self._versions.setdefault(tenant_id, 1)

    def snapshot(self, tenant_id: str) -> EntitlementSnapshot:
        with self._lock:
            if tenant_id not in self._versions:
                raise ResourceNotFound("unknown_tenant", "tenant entitlement aggregate does not exist")
            products = tuple(self._products[tenant_id][key] for key in sorted(self._products[tenant_id], key=lambda item: item.value))
            models = tuple(self._models[tenant_id][key] for key in sorted(self._models[tenant_id]))
            return EntitlementSnapshot(tenant_id, products, models, self._versions[tenant_id])

    def _check_grant(self, tenant_id: str, expected_version: int, idempotency_key: str, fingerprint: str) -> EntitlementMutationResult | None:
        prior = self._idempotency.get(idempotency_key)
        if prior:
            prior_fingerprint, kind, resource = prior
            if prior_fingerprint != fingerprint:
                raise Conflict("idempotency_conflict", "idempotency key was already used for a different entitlement grant")
            item = self._products[tenant_id][Product(resource)] if kind == "product" else self._models[tenant_id][resource]
            return EntitlementMutationResult(item, False)
        if tenant_id not in self._versions:
            raise ResourceNotFound("unknown_tenant", "tenant entitlement aggregate does not exist")
        if self._versions[tenant_id] != expected_version:
            raise StaleVersion("stale_version", "entitlement version is stale")
        return None

    def grant_product(self, entitlement: ProductEntitlement, expected_version: int, idempotency_key: str, fingerprint: str) -> EntitlementMutationResult:
        with self._lock:
            prior = self._check_grant(entitlement.tenant_id, expected_version, idempotency_key, fingerprint)
            if prior:
                return prior
            if entitlement.product in self._products[entitlement.tenant_id]:
                raise Conflict("entitlement_exists", "product entitlement already exists")
            version = self._versions[entitlement.tenant_id] + 1
            stored = replace(entitlement, version=version)
            self._products[entitlement.tenant_id][entitlement.product] = stored
            self._versions[entitlement.tenant_id] = version
            self._idempotency[idempotency_key] = (fingerprint, "product", entitlement.product.value)
            return EntitlementMutationResult(stored, True)

    def revoke_product(self, tenant_id: str, product: Product, expected_version: int) -> EntitlementSnapshot:
        with self._lock:
            if tenant_id not in self._versions:
                raise ResourceNotFound("unknown_tenant", "tenant entitlement aggregate does not exist")
            if self._versions[tenant_id] != expected_version:
                raise StaleVersion("stale_version", "entitlement version is stale")
            if product not in self._products[tenant_id]:
                raise ResourceNotFound("unknown_entitlement", "product entitlement does not exist")
            del self._products[tenant_id][product]
            self._versions[tenant_id] += 1
            return self.snapshot(tenant_id)

    def grant_model(self, entitlement: ModelEntitlement, expected_version: int, idempotency_key: str, fingerprint: str) -> EntitlementMutationResult:
        with self._lock:
            prior = self._check_grant(entitlement.tenant_id, expected_version, idempotency_key, fingerprint)
            if prior:
                return prior
            if entitlement.model in self._models[entitlement.tenant_id]:
                raise Conflict("entitlement_exists", "model entitlement already exists")
            version = self._versions[entitlement.tenant_id] + 1
            stored = replace(entitlement, version=version)
            self._models[entitlement.tenant_id][entitlement.model] = stored
            self._versions[entitlement.tenant_id] = version
            self._idempotency[idempotency_key] = (fingerprint, "model", entitlement.model)
            return EntitlementMutationResult(stored, True)

    def revoke_model(self, tenant_id: str, model: str, expected_version: int) -> EntitlementSnapshot:
        with self._lock:
            if tenant_id not in self._versions:
                raise ResourceNotFound("unknown_tenant", "tenant entitlement aggregate does not exist")
            if self._versions[tenant_id] != expected_version:
                raise StaleVersion("stale_version", "entitlement version is stale")
            if model not in self._models[tenant_id]:
                raise ResourceNotFound("unknown_entitlement", "model entitlement does not exist")
            del self._models[tenant_id][model]
            self._versions[tenant_id] += 1
            return self.snapshot(tenant_id)


class InMemoryPolicyVersionRepository:
    """Test-only append-oriented policy repository."""

    def __init__(self) -> None:
        self._items: dict[str, PolicyDocument] = {}
        self._lock = RLock()

    def create_initial(self, document: PolicyDocument) -> PolicyDocument:
        with self._lock:
            if document.tenant_id in self._items:
                raise Conflict("policy_exists", "tenant policy already exists")
            self._items[document.tenant_id] = document
            return document

    def current(self, tenant_id: str) -> PolicyDocument | None:
        with self._lock:
            return self._items.get(tenant_id)
