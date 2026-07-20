"""Provider-neutral, fail-closed content-safety authorization boundary."""

from __future__ import annotations

from typing import Protocol

from packages.contracts import Product

from .config import GatewayConfig


class ContentSafetyError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ContentSafetyAuthorizer(Protocol):
    def authorize_input(self, tenant_id: str, product: Product, model: str, content: str, request_id: str) -> None: ...

    def authorize_output(self, tenant_id: str, product: Product, model: str, content: str, request_id: str) -> None: ...


class ConfigContentSafetyAuthorizer:
    """Internal/test implementation; production must supply an external authorizer."""

    def __init__(self, config: GatewayConfig) -> None:
        self._config = config

    def _authorize(self, tenant_id: str, content: str, direction: str) -> None:
        policy = self._config.tenant_policies.get(tenant_id)
        if policy is None or not policy.content_safety_enabled:
            raise ContentSafetyError("content_safety_unavailable", "content safety is not enabled for tenant")
        normalized = content.casefold()
        if any(term.casefold() in normalized for term in policy.content_safety_blocked_terms):
            raise ContentSafetyError(f"unsafe_{direction}", f"{direction} content was denied by safety policy")

    def authorize_input(self, tenant_id: str, product: Product, model: str, content: str, request_id: str) -> None:
        self._authorize(tenant_id, content, "input")

    def authorize_output(self, tenant_id: str, product: Product, model: str, content: str, request_id: str) -> None:
        self._authorize(tenant_id, content, "output")
