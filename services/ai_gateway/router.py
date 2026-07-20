"""Pure deterministic provider selection and fallback policy."""

from __future__ import annotations

from dataclasses import dataclass

from packages.contracts import Product

from .config import GatewayConfig, Provider


class RoutingError(ValueError):
    """A request cannot be routed without violating policy."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RouteRequest:
    tenant_id: str
    product: Product
    model: str
    region: str


@dataclass(frozen=True)
class RouteResult:
    provider: str
    model: str
    region: str
    fallback: tuple[str, ...]


class ModelRouter:
    def __init__(self, config: GatewayConfig) -> None:
        self._config = config

    def route(self, request: RouteRequest) -> RouteResult:
        policy = self._config.tenant_policies.get(request.tenant_id)
        if policy is None:
            raise RoutingError("unknown_tenant", "tenant has no routing policy")
        if request.region not in policy.allowed_regions:
            raise RoutingError("region_not_allowed", "requested region is not allowed for tenant")

        eligible: list[Provider] = []
        for provider in self._config.providers:
            if provider.id not in policy.allowed_providers:
                continue
            if provider.id in policy.blocked_providers:
                continue
            if request.region not in provider.regions or request.model not in provider.models:
                continue
            eligible.append(provider)

        if not eligible:
            raise RoutingError("no_eligible_provider", "no provider satisfies tenant, model, and regional policy")
        return RouteResult(
            provider=eligible[0].id,
            model=request.model,
            region=request.region,
            fallback=tuple(provider.id for provider in eligible[1:]),
        )
