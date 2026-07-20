"""Authenticated HTTP boundary for routing and secure provider execution."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from packages.contracts import Product

from .audit import AuditSink, JsonAuditSink, audit_event
from .auth import AuthenticationError, Authenticator, HmacBearerAuthenticator, VerifiedIdentity
from .authorization import AuthorizationError, EntitlementAuthorizer
from .billing import BillingError, ConfigBillingAuthorizer
from .config import ConfigurationError, GatewayConfig, load_config
from .content_safety import ConfigContentSafetyAuthorizer, ContentSafetyAuthorizer, ContentSafetyError
from .execution import SecureExecutionRequest, SecureExecutionService
from .providers import AzureOpenAIAdapter, OpenAIAdapter, ProviderAdapter, ProviderError
from .resilience import ProviderUnavailable, ResiliencePolicy, ResilientProviderExecutor
from .router import ModelRouter, RouteRequest, RoutingError
from .secrets import EnvironmentSecretManager, SecretError, SecretManager

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(os.environ.get("UWO_GATEWAY_CONFIG", ROOT / "infrastructure/config/ai-gateway.json"))
REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@dataclass(frozen=True)
class GatewayDependencies:
    router: ModelRouter
    authenticator: Authenticator
    execution: SecureExecutionService
    audit: AuditSink


class GatewayHandler(BaseHTTPRequestHandler):
    dependencies: GatewayDependencies
    server_version = "UWOAIGateway/0.2"

    def _request_id(self) -> str:
        supplied = self.headers.get("X-Request-ID", "")
        return supplied if REQUEST_ID.fullmatch(supplied) else str(uuid.uuid4())

    def _respond(self, status: HTTPStatus, body: dict[str, Any], request_id: str) -> None:
        payload = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Request-ID", request_id)
        if status == HTTPStatus.UNAUTHORIZED:
            self.send_header("WWW-Authenticate", 'Bearer realm="uwo-ai-gateway"')
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, status: HTTPStatus, code: str, message: str, request_id: str) -> None:
        self._respond(status, {"error": {"code": code, "message": message}, "request_id": request_id}, request_id)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 65_536:
            raise ValueError("request body must be between 1 and 65536 bytes")
        body = json.loads(self.rfile.read(length))
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        return body

    def _identity(self) -> VerifiedIdentity:
        return self.dependencies.authenticator.authenticate(self.headers.get("Authorization", ""))

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler interface
        request_id = self._request_id()
        if self.path == "/healthz":
            self._respond(HTTPStatus.OK, {"status": "ok", "service": "ai-gateway", "request_id": request_id}, request_id)
            return
        self._error(HTTPStatus.NOT_FOUND, "not_found", "route not found", request_id)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler interface
        request_id = self._request_id()
        if self.path not in {"/v1/route", "/v1/execute"}:
            self._error(HTTPStatus.NOT_FOUND, "not_found", "route not found", request_id)
            return
        identity: VerifiedIdentity | None = None
        try:
            identity = self._identity()
            body = self._body()
            product = Product(body["product"])
            tenant_id = body["tenant_id"]
            model = body["model"]
            region = body["region"]
            if not all(isinstance(value, str) and value for value in (tenant_id, model, region)):
                raise ValueError("tenant_id, model, and region must be non-empty strings")
            if self.path == "/v1/route":
                route_request = RouteRequest(tenant_id, product, model, region)
                self.dependencies.execution.authorize_route(identity, route_request, request_id)
                result: Any = self.dependencies.router.route(route_request)
            else:
                prompt = body["prompt"]
                if not isinstance(prompt, str) or not prompt or len(prompt) > 32_768:
                    raise ValueError("prompt must be a non-empty string of at most 32768 characters")
                result = self.dependencies.execution.execute(identity, SecureExecutionRequest(request_id, tenant_id, product, model, region, prompt))
        except AuthenticationError as exc:
            self.dependencies.audit.emit(audit_event("request.authentication", request_id, "denied", reason_code=exc.code))
            self._error(HTTPStatus.UNAUTHORIZED, exc.code, str(exc), request_id)
            return
        except AuthorizationError as exc:
            self.dependencies.audit.emit(audit_event("request.authorization", request_id, "denied", tenant_id=identity.tenant_id if identity else None, subject=identity.subject if identity else None, reason_code=exc.code))
            self._error(HTTPStatus.FORBIDDEN, exc.code, str(exc), request_id)
            return
        except BillingError as exc:
            self.dependencies.audit.emit(audit_event("billing.authorization", request_id, "denied", tenant_id=identity.tenant_id if identity else None, reason_code=exc.code))
            self._error(HTTPStatus.PAYMENT_REQUIRED, exc.code, str(exc), request_id)
            return
        except ContentSafetyError as exc:
            self.dependencies.audit.emit(audit_event("content_safety.authorization", request_id, "denied", tenant_id=identity.tenant_id if identity else None, reason_code=exc.code))
            self._error(HTTPStatus.UNPROCESSABLE_ENTITY, exc.code, str(exc), request_id)
            return
        except ProviderError as exc:
            status = HTTPStatus.UNPROCESSABLE_ENTITY if exc.code == "provider_refusal" else HTTPStatus.BAD_GATEWAY
            self.dependencies.audit.emit(audit_event("provider.execution", request_id, "failed", tenant_id=identity.tenant_id if identity else None, reason_code=exc.code))
            self._error(status, exc.code, str(exc), request_id)
            return
        except RoutingError as exc:
            self.dependencies.audit.emit(audit_event("route.selection", request_id, "denied", tenant_id=identity.tenant_id if identity else None, reason_code=exc.code))
            self._error(HTTPStatus.FORBIDDEN, exc.code, str(exc), request_id)
            return
        except (ProviderUnavailable, SecretError) as exc:
            self.dependencies.audit.emit(audit_event("provider.execution", request_id, "failed", tenant_id=identity.tenant_id if identity else None, reason_code="provider_unavailable"))
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "provider_unavailable", str(exc), request_id)
            return
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc), request_id)
            return
        self._respond(HTTPStatus.OK, asdict(result), request_id)

    def log_message(self, format: str, *args: object) -> None:
        print(json.dumps({"event": "http_request", "message": format % args}))


def build_adapters(config: GatewayConfig, secrets: SecretManager) -> dict[str, ProviderAdapter]:
    adapters: dict[str, ProviderAdapter] = {}
    for provider in config.providers:
        if provider.adapter == "azure-openai":
            adapters[provider.id] = AzureOpenAIAdapter(provider.id, provider.endpoint, provider.deployment or "", provider.secret_ref, secrets)
        elif provider.adapter == "openai":
            adapters[provider.id] = OpenAIAdapter(provider.id, provider.endpoint, provider.secret_ref, secrets)
    return adapters


def build_dependencies(config: GatewayConfig, authenticator: Authenticator, adapters: dict[str, ProviderAdapter], content_safety: ContentSafetyAuthorizer, audit: AuditSink) -> GatewayDependencies:
    router = ModelRouter(config)
    execution = SecureExecutionService(
        router,
        EntitlementAuthorizer(config),
        ConfigBillingAuthorizer(config),
        content_safety,
        ResilientProviderExecutor(adapters, ResiliencePolicy()),
        audit,
    )
    return GatewayDependencies(router, authenticator, execution, audit)


def create_server(host: str = "127.0.0.1", port: int = 8080, content_safety: ContentSafetyAuthorizer | None = None) -> ThreadingHTTPServer:
    production = os.environ.get("UWO_ENVIRONMENT", "development").casefold() == "production"
    if production and content_safety is None:
        raise ConfigurationError("production content-safety integration is required before startup")
    config = load_config(CONFIG_PATH)
    content_safety = content_safety or ConfigContentSafetyAuthorizer(config)
    secrets = EnvironmentSecretManager()
    authenticator = HmacBearerAuthenticator(secrets.get_secret("env://UWO_AUTH_SIGNING_KEY"))
    GatewayHandler.dependencies = build_dependencies(config, authenticator, build_adapters(config, secrets), content_safety, JsonAuditSink())
    return ThreadingHTTPServer((host, port), GatewayHandler)


def main() -> None:
    try:
        server = create_server(os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8080")))
    except (ConfigurationError, SecretError, ValueError) as exc:
        raise SystemExit(f"gateway startup failed: {exc}") from exc
    server.serve_forever()


if __name__ == "__main__":
    main()
