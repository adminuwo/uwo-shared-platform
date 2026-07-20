"""Secure provider execution orchestration."""

from __future__ import annotations

from dataclasses import dataclass

from packages.contracts import Product

from .audit import AuditSink, audit_event
from .auth import VerifiedIdentity
from .authorization import EntitlementAuthorizer
from .billing import BillingAuthorizer
from .content_safety import ContentSafetyAuthorizer
from .providers import ProviderRequest
from .resilience import ResilientProviderExecutor
from .router import ModelRouter, RouteRequest


@dataclass(frozen=True)
class SecureExecutionRequest:
    request_id: str
    tenant_id: str
    product: Product
    model: str
    region: str
    prompt: str


@dataclass(frozen=True)
class SecureExecutionResult:
    request_id: str
    provider: str
    model: str
    region: str
    output_text: str
    provider_request_id: str | None


class SecureExecutionService:
    def __init__(self, router: ModelRouter, entitlements: EntitlementAuthorizer, billing: BillingAuthorizer, content_safety: ContentSafetyAuthorizer, providers: ResilientProviderExecutor, audit: AuditSink) -> None:
        self._router = router
        self._entitlements = entitlements
        self._billing = billing
        self._content_safety = content_safety
        self._providers = providers
        self._audit = audit

    def authorize_route(self, identity: VerifiedIdentity, request: RouteRequest, request_id: str) -> None:
        self._entitlements.authorize(identity, request.tenant_id, request.product, request.model)
        self._audit.emit(audit_event("route.authorization", request_id, "allowed", tenant_id=identity.tenant_id, subject=identity.subject, product=request.product.value, model=request.model))

    def execute(self, identity: VerifiedIdentity, request: SecureExecutionRequest) -> SecureExecutionResult:
        self._entitlements.authorize(identity, request.tenant_id, request.product, request.model)
        self._billing.authorize(identity.tenant_id, request.product, request.model, request.request_id)
        self._content_safety.authorize_input(identity.tenant_id, request.product, request.model, request.prompt, request.request_id)
        route = self._router.route(RouteRequest(request.tenant_id, request.product, request.model, request.region))
        self._audit.emit(audit_event("provider.execution", request.request_id, "started", tenant_id=identity.tenant_id, subject=identity.subject, product=request.product.value, model=request.model, provider=route.provider))
        result = self._providers.execute(
            (route.provider,) + route.fallback,
            ProviderRequest(request.request_id, identity.tenant_id, request.model, request.prompt),
        )
        self._content_safety.authorize_output(identity.tenant_id, request.product, request.model, result.response.output_text, request.request_id)
        self._audit.emit(audit_event("provider.execution", request.request_id, "succeeded", tenant_id=identity.tenant_id, subject=identity.subject, product=request.product.value, model=request.model, provider=result.provider_id))
        return SecureExecutionResult(
            request_id=request.request_id,
            provider=result.provider_id,
            model=request.model,
            region=request.region,
            output_text=result.response.output_text,
            provider_request_id=result.response.provider_request_id,
        )
