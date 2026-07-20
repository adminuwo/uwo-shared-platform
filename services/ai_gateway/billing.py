"""Billing and credit authorization boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from packages.contracts import Product

from .config import GatewayConfig


class BillingError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class BillingAuthorization:
    authorization_id: str


class BillingAuthorizer(Protocol):
    def authorize(self, tenant_id: str, product: Product, model: str, request_id: str) -> BillingAuthorization: ...


class ConfigBillingAuthorizer:
    """Bootstrap bridge; replace with the billing service adapter in Phase 3."""

    def __init__(self, config: GatewayConfig) -> None:
        self._config = config

    def authorize(self, tenant_id: str, product: Product, model: str, request_id: str) -> BillingAuthorization:
        policy = self._config.tenant_policies.get(tenant_id)
        if policy is None or not policy.billing_authorized:
            raise BillingError("credits_not_authorized", "billing or credits did not authorize this request")
        return BillingAuthorization(authorization_id=f"config:{request_id}")
