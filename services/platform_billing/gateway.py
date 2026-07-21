"""Provider-neutral AI Gateway billing lifecycle contract and service adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from threading import RLock

from packages.contracts import BillingDecision, Product, UsageDimensions, VerifiedSubjectIdentity

from .service import PlatformBillingService


@dataclass(frozen=True)
class GatewayReservation:
    reservation_id: str
    tenant_id: str
    request_id: str
    reservation_version: int = 1
    balance_version: int = 1


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
        self._lock = RLock()
        self._reservation_replays: dict[tuple[str, str], tuple[tuple[str, str, int], GatewayReservation]] = {}

    def authorize_estimated_charge(self, tenant_id: str, product: Product, model: str, request_id: str) -> BillingDecision:
        return self._service.authorize_estimated_charge(self._executor, tenant_id, self._estimated_microunits, request_id)

    def reserve(self, tenant_id: str, product: Product, model: str, request_id: str) -> GatewayReservation:
        replay_key = (tenant_id, request_id)
        fingerprint = (product.value, model, self._estimated_microunits)
        with self._lock:
            existing = self._reservation_replays.get(replay_key)
            if existing is not None:
                if existing[0] != fingerprint:
                    from .errors import Conflict
                    raise Conflict("idempotency_conflict", "gateway request ID was reused with different billing input")
                return existing[1]
        expires = (datetime.now(timezone.utc) + timedelta(seconds=self._reservation_seconds)).isoformat()
        result = self._service.reserve_for_gateway(self._executor, tenant_id, product, model, request_id, self._estimated_microunits, expires, f"gateway-reserve:{request_id}")
        receipt = GatewayReservation(result.reservation.reservation_id, tenant_id, request_id, result.reservation.version, result.balance.version)
        with self._lock:
            self._reservation_replays[replay_key] = (fingerprint, receipt)
        return receipt

    def capture(self, reservation: GatewayReservation, provider_id: str, provider_model_id: str | None, region: str, provider_request_id: str | None, input_tokens: int, output_tokens: int, total_tokens: int) -> None:
        result = self._service.capture(
            self._executor, reservation.reservation_id, _usage_id(reservation.request_id), provider_id,
            provider_model_id, region, provider_request_id, UsageDimensions(input_tokens, output_tokens, total_tokens),
            reservation.reservation_version, reservation.balance_version,
            f"gateway-capture:{reservation.request_id}", reservation.request_id,
        )
        if result.reservation.captured_microunits + result.reservation.released_microunits < result.reservation.estimated_microunits:
            self._service.release(self._executor, reservation.reservation_id, result.reservation.version, result.balance.version, f"gateway-release-after-capture:{reservation.request_id}", reservation.request_id)

    def release_on_failure(self, reservation: GatewayReservation) -> None:
        self._service.release(self._executor, reservation.reservation_id, reservation.reservation_version, reservation.balance_version, f"gateway-release:{reservation.request_id}", reservation.request_id)


def _usage_id(request_id: str) -> str:
    import hashlib
    return f"usage:{hashlib.sha256(request_id.encode()).hexdigest()[:32]}"
