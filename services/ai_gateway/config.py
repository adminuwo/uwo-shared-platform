"""Load and validate AI gateway policy configuration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packages.contracts import Product


class ConfigurationError(ValueError):
    """Configuration is missing or internally inconsistent."""


@dataclass(frozen=True)
class Provider:
    id: str
    regions: frozenset[str]
    models: frozenset[str]
    priority: int
    adapter: str
    endpoint: str
    secret_ref: str
    deployment: str | None = None
    api_version: str | None = None


@dataclass(frozen=True)
class TenantPolicy:
    allowed_providers: frozenset[str]
    blocked_providers: frozenset[str]
    allowed_regions: frozenset[str]
    allowed_products: frozenset[Product]
    allowed_models: frozenset[str]
    billing_authorized: bool


@dataclass(frozen=True)
class GatewayConfig:
    providers: tuple[Provider, ...]
    tenant_policies: dict[str, TenantPolicy]


def _required_strings(value: Any, path: str) -> frozenset[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ConfigurationError(f"{path} must be a non-empty list of strings")
    return frozenset(value)


def load_config(path: Path) -> GatewayConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"cannot load gateway configuration: {exc}") from exc

    providers: list[Provider] = []
    seen_priorities: set[int] = set()
    seen_provider_ids: set[str] = set()
    for index, item in enumerate(raw.get("providers", [])):
        try:
            provider = Provider(
                id=item["id"],
                regions=_required_strings(item["regions"], f"providers[{index}].regions"),
                models=_required_strings(item["models"], f"providers[{index}].models"),
                priority=int(item["priority"]),
                adapter=item["adapter"],
                endpoint=item["endpoint"],
                secret_ref=item["secret_ref"],
                deployment=item.get("deployment"),
                api_version=item.get("api_version"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigurationError(f"invalid provider at index {index}: {exc}") from exc
        if not all(isinstance(value, str) and value for value in (provider.id, provider.adapter, provider.endpoint, provider.secret_ref)):
            raise ConfigurationError(f"provider at index {index} has invalid string fields")
        if provider.id in seen_provider_ids:
            raise ConfigurationError(f"provider id must be unique: {provider.id!r}")
        seen_provider_ids.add(provider.id)
        if provider.priority in seen_priorities:
            raise ConfigurationError("provider priorities must be unique for deterministic routing")
        seen_priorities.add(provider.priority)
        if provider.adapter not in {"azure-openai", "openai"}:
            raise ConfigurationError(f"provider {provider.id!r} has unsupported adapter {provider.adapter!r}")
        if provider.adapter == "azure-openai" and (not provider.deployment or not provider.api_version):
            raise ConfigurationError(f"provider {provider.id!r} requires deployment and api_version")
        if not provider.endpoint.startswith("https://") or not provider.secret_ref.startswith("env://"):
            raise ConfigurationError(f"provider {provider.id!r} must use an HTTPS endpoint and env secret reference")
        providers.append(provider)
    if not providers:
        raise ConfigurationError("at least one provider is required")

    provider_ids = {provider.id for provider in providers}
    policies: dict[str, TenantPolicy] = {}
    for tenant_id, item in raw.get("tenant_policies", {}).items():
        if not isinstance(tenant_id, str) or not tenant_id or not isinstance(item, dict):
            raise ConfigurationError("tenant policy identifiers and values must be valid")
        try:
            policy = TenantPolicy(
                allowed_providers=_required_strings(item.get("allowed_providers"), f"tenant_policies.{tenant_id}.allowed_providers"),
                blocked_providers=frozenset(item.get("blocked_providers", [])),
                allowed_regions=_required_strings(item.get("allowed_regions"), f"tenant_policies.{tenant_id}.allowed_regions"),
                allowed_products=frozenset(Product(value) for value in _required_strings(item.get("allowed_products"), f"tenant_policies.{tenant_id}.allowed_products")),
                allowed_models=_required_strings(item.get("allowed_models"), f"tenant_policies.{tenant_id}.allowed_models"),
                billing_authorized=item.get("billing_authorized") is True,
            )
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"invalid policy for tenant {tenant_id!r}: {exc}") from exc
        unknown = (policy.allowed_providers | policy.blocked_providers) - provider_ids
        if unknown:
            raise ConfigurationError(f"tenant {tenant_id!r} references unknown providers: {sorted(unknown)}")
        policies[tenant_id] = policy
    if not policies:
        raise ConfigurationError("at least one tenant policy is required")

    return GatewayConfig(tuple(sorted(providers, key=lambda item: (item.priority, item.id))), policies)
