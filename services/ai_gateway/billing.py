"""Billing and credit authorization boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from packages.contracts import BillingDecision, Product, utc_now
from services.platform_billing.gateway import GatewayBilling, GatewayReservation

from .config import GatewayConfig


class BillingError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BillingCompensationError(RuntimeError):
    """A reservation remains discoverable because failure compensation did not complete."""

    code = "billing_compensation_failed"

    def __init__(self) -> None:
        super().__init__("billing compensation failed; the reservation requires recovery")


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


class AuthorizationOnlyGatewayBilling:
    """Compatibility lifecycle for the bootstrap config authorizer; it never creates charges."""

    def __init__(self, authorizer: BillingAuthorizer) -> None:
        self._authorizer = authorizer

    def authorize_estimated_charge(self, tenant_id: str, product: Product, model: str, request_id: str) -> BillingDecision:
        self._authorizer.authorize(tenant_id, product, model, request_id)
        return BillingDecision(f"config:{request_id}", tenant_id, True, 0, "config_authorized", utc_now())

    def reserve(self, tenant_id: str, product: Product, model: str, request_id: str) -> GatewayReservation:
        return GatewayReservation(f"config:{request_id}", tenant_id, request_id)

    def capture(self, reservation: GatewayReservation, provider_id: str, provider_model_id: str | None, region: str, provider_request_id: str | None, input_tokens: int, output_tokens: int, total_tokens: int) -> None:
        return None

    def release_on_failure(self, reservation: GatewayReservation) -> None:
        return None
