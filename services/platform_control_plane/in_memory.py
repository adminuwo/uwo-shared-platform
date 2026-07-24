"""Thread-safe, rollback-capable in-memory repositories for tests only."""

from __future__ import annotations

from dataclasses import replace
from threading import RLock
from typing import Any

from packages.contracts import ModelEntitlement, PolicyDocument, Product, ProductEntitlement, Role, Tenant, TenantMembership
from services.data_service_common import InMemoryOutbox

from .errors import Conflict, RepositoryIntegrityError, ResourceNotFound, StaleVersion
from .repositories import EntitlementSnapshot, IdempotencyRecord, IdempotencyScope, Page


class FailureInjector:
    """Deterministic one-shot persistence failure injection for tests."""

    def __init__(self) -> None:
        self._failure_point: str | None = None

    def fail_next(self, failure_point: str) -> None:
        self._failure_point = failure_point

    def trigger(self, failure_point: str) -> None:
        if self._failure_point == failure_point:
            self._failure_point = None
            raise RepositoryIntegrityError("injected repository integrity failure")


class InMemoryTenantRepository:
    """Test-only tenant repository; production must inject durable storage."""

    def __init__(self, failures: FailureInjector | None = None) -> None:
        self._items: dict[str, Tenant] = {}
        self._lock = RLock()
        self._failures = failures or FailureInjector()

    def create(self, tenant: Tenant) -> Tenant:
        with self._lock:
            if tenant.tenant_id in self._items:
                raise Conflict("tenant_exists", "tenant already exists")
            self._items[tenant.tenant_id] = tenant
            self._failures.trigger("tenant_write")
            return tenant

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

    def _snapshot(self) -> dict[str, Tenant]:
        with self._lock:
            return dict(self._items)

    def _restore(self, snapshot: dict[str, Tenant]) -> None:
        with self._lock:
            self._items = dict(snapshot)


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

    def _snapshot(self) -> dict[tuple[str, str], TenantMembership]:
        with self._lock:
            return dict(self._items)

    def _restore(self, snapshot: dict[tuple[str, str], TenantMembership]) -> None:
        with self._lock:
            self._items = dict(snapshot)


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

    def __init__(self, failures: FailureInjector | None = None) -> None:
        self._products: dict[str, dict[Product, ProductEntitlement]] = {}
        self._models: dict[str, dict[str, ModelEntitlement]] = {}
        self._versions: dict[str, int] = {}
        self._lock = RLock()
        self._failures = failures or FailureInjector()

    def initialize(self, tenant_id: str) -> None:
        with self._lock:
            if tenant_id in self._versions:
                raise Conflict("entitlement_aggregate_exists", "tenant entitlement aggregate already exists")
            self._products[tenant_id] = {}
            self._models[tenant_id] = {}
            self._versions[tenant_id] = 1
            self._failures.trigger("entitlement_initialization")

    def snapshot(self, tenant_id: str) -> EntitlementSnapshot:
        with self._lock:
            if tenant_id not in self._versions:
                raise ResourceNotFound("unknown_tenant", "tenant entitlement aggregate does not exist")
            products = tuple(self._products[tenant_id][key] for key in sorted(self._products[tenant_id], key=lambda item: item.value))
            models = tuple(self._models[tenant_id][key] for key in sorted(self._models[tenant_id]))
            return EntitlementSnapshot(tenant_id, products, models, self._versions[tenant_id])

    def grant_product(self, entitlement: ProductEntitlement, expected_version: int) -> ProductEntitlement:
        with self._lock:
            tenant_id = entitlement.tenant_id
            if tenant_id not in self._versions:
                raise ResourceNotFound("unknown_tenant", "tenant entitlement aggregate does not exist")
            if self._versions[tenant_id] != expected_version:
                raise StaleVersion("stale_version", "entitlement version is stale")
            if entitlement.product in self._products[tenant_id]:
                raise Conflict("entitlement_exists", "product entitlement already exists")
            version = self._versions[tenant_id] + 1
            stored = replace(entitlement, version=version)
            self._products[tenant_id][entitlement.product] = stored
            self._versions[tenant_id] = version
            return stored

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

    def grant_model(self, entitlement: ModelEntitlement, expected_version: int) -> ModelEntitlement:
        with self._lock:
            tenant_id = entitlement.tenant_id
            if tenant_id not in self._versions:
                raise ResourceNotFound("unknown_tenant", "tenant entitlement aggregate does not exist")
            if self._versions[tenant_id] != expected_version:
                raise StaleVersion("stale_version", "entitlement version is stale")
            if entitlement.model in self._models[tenant_id]:
                raise Conflict("entitlement_exists", "model entitlement already exists")
            version = self._versions[tenant_id] + 1
            stored = replace(entitlement, version=version)
            self._models[tenant_id][entitlement.model] = stored
            self._versions[tenant_id] = version
            return stored

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

    def _snapshot(self) -> tuple[dict[str, dict[Product, ProductEntitlement]], dict[str, dict[str, ModelEntitlement]], dict[str, int]]:
        with self._lock:
            return (
                {tenant: dict(items) for tenant, items in self._products.items()},
                {tenant: dict(items) for tenant, items in self._models.items()},
                dict(self._versions),
            )

    def _restore(self, snapshot: tuple[dict[str, dict[Product, ProductEntitlement]], dict[str, dict[str, ModelEntitlement]], dict[str, int]]) -> None:
        with self._lock:
            products, models, versions = snapshot
            self._products = {tenant: dict(items) for tenant, items in products.items()}
            self._models = {tenant: dict(items) for tenant, items in models.items()}
            self._versions = dict(versions)


class InMemoryPolicyVersionRepository:
    """Test-only append-oriented policy repository."""

    def __init__(self, failures: FailureInjector | None = None) -> None:
        self._items: dict[str, PolicyDocument] = {}
        self._lock = RLock()
        self._failures = failures or FailureInjector()

    def create_initial(self, document: PolicyDocument) -> PolicyDocument:
        with self._lock:
            if document.tenant_id in self._items:
                raise Conflict("policy_exists", "tenant policy already exists")
            self._items[document.tenant_id] = document
            self._failures.trigger("policy_initialization")
            return document

    def current(self, tenant_id: str) -> PolicyDocument | None:
        with self._lock:
            return self._items.get(tenant_id)

    def _snapshot(self) -> dict[str, PolicyDocument]:
        with self._lock:
            return dict(self._items)

    def _restore(self, snapshot: dict[str, PolicyDocument]) -> None:
        with self._lock:
            self._items = dict(snapshot)


class InMemoryIdempotencyRepository:
    """Test-only scoped ledger storing immutable original operation results."""

    def __init__(self, failures: FailureInjector | None = None) -> None:
        self._records: dict[tuple[str, str, str, str], IdempotencyRecord] = {}
        self._lock = RLock()
        self._failures = failures or FailureInjector()

    @staticmethod
    def _record_key(scope: IdempotencyScope, key: str) -> tuple[str, str, str, str]:
        return (scope.operation, scope.tenant_id, scope.actor_subject, key)

    def get(self, scope: IdempotencyScope, key: str) -> IdempotencyRecord | None:
        with self._lock:
            return self._records.get(self._record_key(scope, key))

    def put(self, record: IdempotencyRecord) -> IdempotencyRecord:
        with self._lock:
            key = self._record_key(record.scope, record.key)
            if key in self._records:
                raise Conflict("idempotency_conflict", "idempotency record already exists")
            self._records[key] = record
            self._failures.trigger("idempotency_persistence")
            return record

    def _snapshot(self) -> dict[tuple[str, str, str, str], IdempotencyRecord]:
        with self._lock:
            return dict(self._records)

    def _restore(self, snapshot: dict[tuple[str, str, str, str], IdempotencyRecord]) -> None:
        with self._lock:
            self._records = dict(snapshot)


class InMemoryUnitOfWork:
    """Test-only transaction over all control-plane repositories."""

    def __init__(
        self,
        tenants: InMemoryTenantRepository,
        memberships: InMemoryMembershipRepository,
        roles: InMemoryRoleRepository,
        entitlements: InMemoryEntitlementRepository,
        policies: InMemoryPolicyVersionRepository,
        idempotency: InMemoryIdempotencyRepository,
        outbox: InMemoryOutbox,
        transaction_lock: RLock,
    ) -> None:
        self.tenants = tenants
        self.memberships = memberships
        self.roles = roles
        self.entitlements = entitlements
        self.policies = policies
        self.idempotency = idempotency
        self.outbox = outbox
        self._transaction_lock = transaction_lock
        self._repository_locks = (tenants._lock, memberships._lock, entitlements._lock, policies._lock, idempotency._lock, outbox._lock)
        self._snapshots: tuple[Any, ...] | None = None
        self._committed = False

    def __enter__(self) -> "InMemoryUnitOfWork":
        self._transaction_lock.acquire()
        for repository_lock in self._repository_locks:
            repository_lock.acquire()
        self._snapshots = (
            self.tenants._snapshot(),
            self.memberships._snapshot(),
            self.entitlements._snapshot(),
            self.policies._snapshot(),
            self.idempotency._snapshot(),
            self.outbox.snapshot(),
        )
        return self

    def commit(self) -> None:
        self._committed = True

    def rollback(self) -> None:
        if self._snapshots is None:
            return
        tenants, memberships, entitlements, policies, idempotency, outbox = self._snapshots
        self.tenants._restore(tenants)
        self.memberships._restore(memberships)
        self.entitlements._restore(entitlements)
        self.policies._restore(policies)
        self.idempotency._restore(idempotency)
        self.outbox.restore(outbox)
        self._snapshots = None

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        try:
            if exc_type is not None or not self._committed:
                self.rollback()
        finally:
            for repository_lock in reversed(self._repository_locks):
                repository_lock.release()
            self._transaction_lock.release()


class InMemoryUnitOfWorkFactory:
    def __init__(
        self,
        tenants: InMemoryTenantRepository,
        memberships: InMemoryMembershipRepository,
        roles: InMemoryRoleRepository,
        entitlements: InMemoryEntitlementRepository,
        policies: InMemoryPolicyVersionRepository,
        idempotency: InMemoryIdempotencyRepository,
        outbox: InMemoryOutbox | None = None,
    ) -> None:
        self.outbox = outbox or InMemoryOutbox()
        self._repositories = (tenants, memberships, roles, entitlements, policies, idempotency, self.outbox)
        self._transaction_lock = RLock()

    def __call__(self) -> InMemoryUnitOfWork:
        return InMemoryUnitOfWork(*self._repositories, self._transaction_lock)
