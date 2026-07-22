"""Provider-neutral AI Gateway billing lifecycle contract and service adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from packages.contracts import BillingDecision, Product, UsageDimensions, VerifiedSubjectIdentity

from .service import PlatformBillingService


@dataclass(frozen=True)
class GatewayReservation:
    reservation_id: str
    tenant_id: str
    request_id: str


class GatewayBilling(Protocol):
    def authorize_estimated_charge(self, tenant_id: str, product: Product, model: str, request_id: str) -> BillingDecision: ...
    def reserve(self, tenant_id: str, product: Product, model: str, request_id: str) -> GatewayReservation: ...
    def capture(self, reservation: GatewayReservation, provider_id: str, provider_model_id: str | None, region: str, provider_request_id: str | None, input_tokens: int, output_tokens: int, total_tokens: int) -> None: ...
    def release_on_failure(self, reservation: GatewayReservation) -> None: ...


class ServiceGatewayBilling:
    """In-process integration adapter; a remote client can implement the same contract."""

    def __init__(self, service: PlatformBillingService, executor: VerifiedSubjectIdentity, estimated_microunits: int, reservation_seconds: int = 120) -> None:
        self._service = service
        self._executor = executor
        self._estimated_microunits = estimated_microunits
        self._reservation_seconds = reservation_seconds

    def authorize_estimated_charge(self, tenant_id: str, product: Product, model: str, request_id: str) -> BillingDecision:
        return self._service.authorize_estimated_charge(self._executor, tenant_id, self._estimated_microunits, request_id)

    def reserve(self, tenant_id: str, product: Product, model: str, request_id: str) -> GatewayReservation:
        result = self._service.reserve_for_gateway(self._executor, tenant_id, product, model, request_id, self._estimated_microunits, self._reservation_seconds, f"gateway-reserve:{request_id}")
        return GatewayReservation(result.reservation.reservation_id, tenant_id, request_id)

    def capture(self, reservation: GatewayReservation, provider_id: str, provider_model_id: str | None, region: str, provider_request_id: str | None, input_tokens: int, output_tokens: int, total_tokens: int) -> None:
        self._service.capture_for_gateway(
            self._executor, reservation.tenant_id, reservation.reservation_id, _usage_id(reservation.request_id), provider_id,
            provider_model_id, region, provider_request_id, UsageDimensions(input_tokens, output_tokens, total_tokens),
            f"gateway-capture:{reservation.request_id}", reservation.request_id,
        )

    def release_on_failure(self, reservation: GatewayReservation) -> None:
        self._service.release_for_gateway(self._executor, reservation.tenant_id, reservation.reservation_id, f"gateway-release:{reservation.request_id}", reservation.request_id)


def _usage_id(request_id: str) -> str:
    import hashlib
    return f"usage:{hashlib.sha256(request_id.encode()).hexdigest()[:32]}"
