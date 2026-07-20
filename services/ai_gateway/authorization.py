"""Tenant identity binding and product/model entitlement checks."""

from __future__ import annotations

from packages.contracts import Product

from .auth import VerifiedIdentity
from .config import GatewayConfig


class AuthorizationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EntitlementAuthorizer:
    def __init__(self, config: GatewayConfig) -> None:
        self._config = config

    def authorize(self, identity: VerifiedIdentity, requested_tenant: str, product: Product, model: str) -> None:
        if identity.tenant_id != requested_tenant:
            raise AuthorizationError("tenant_identity_mismatch", "verified tenant does not match requested tenant")
        policy = self._config.tenant_policies.get(identity.tenant_id)
        if policy is None:
            raise AuthorizationError("unknown_tenant", "tenant has no authorization policy")
        if product not in policy.allowed_products:
            raise AuthorizationError("product_not_entitled", "tenant is not entitled to the requested product")
        if model not in policy.allowed_models:
            raise AuthorizationError("model_not_entitled", "tenant is not entitled to the requested model")
