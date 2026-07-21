"""Provider-neutral effective-entitlement lookup contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from packages.contracts import Product, TenantStatus

from .errors import AuthorizationDenied, ResourceNotFound
from .repositories import EntitlementRepository, TenantRepository


@dataclass(frozen=True)
class EffectiveEntitlements:
    tenant_id: str
    products: tuple[str, ...]
    models: tuple[str, ...]
    version: int


class EntitlementLookup(Protocol):
    def get_effective_entitlements(self, tenant_id: str) -> EffectiveEntitlements: ...
    def authorize(self, tenant_id: str, product: Product, model: str) -> None: ...


class RepositoryEntitlementLookup:
    """Provider-neutral adapter suitable for later AI Gateway integration."""

    def __init__(self, tenants: TenantRepository, entitlements: EntitlementRepository) -> None:
        self._tenants = tenants
        self._entitlements = entitlements

    def get_effective_entitlements(self, tenant_id: str) -> EffectiveEntitlements:
        tenant = self._tenants.get(tenant_id)
        if tenant is None:
            raise ResourceNotFound("unknown_tenant", "tenant does not exist")
        if tenant.status is not TenantStatus.ACTIVE:
            raise AuthorizationDenied("tenant_suspended", "suspended tenant has no effective entitlements")
        snapshot = self._entitlements.snapshot(tenant_id)
        return EffectiveEntitlements(
            tenant_id,
            tuple(item.product.value for item in snapshot.products),
            tuple(item.model for item in snapshot.models),
            snapshot.version,
        )

    def authorize(self, tenant_id: str, product: Product, model: str) -> None:
        effective = self.get_effective_entitlements(tenant_id)
        if product.value not in effective.products:
            raise AuthorizationDenied("product_not_entitled", "tenant is not entitled to the product")
        if model not in effective.models:
            raise AuthorizationDenied("model_not_entitled", "tenant is not entitled to the model")
