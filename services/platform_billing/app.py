"""Authenticated versioned HTTP boundary for the billing control service."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

from packages.contracts import BillingAccountStatus, Product, UsageDimensions, VerifiedSubjectIdentity
from services.platform_control_plane.auth import AuthenticationError, Authenticator

from .audit import AuditSink, audit_event
from .errors import AuthorizationDenied, Conflict, InvalidRequest, PaymentRequired, ResourceNotFound
from .service import PlatformBillingService

REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
MAX_REQUEST_BYTES = 65_536


@dataclass(frozen=True)
class BillingDependencies:
    service: PlatformBillingService
    authenticator: Authenticator
    audit: AuditSink


class RequestTooLarge(InvalidRequest):
    pass


def _json(value: Any) -> Any:
    if is_dataclass(value):
        return {item.name: _json(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(item) for item in value]
    return value


def _handler(dependencies: BillingDependencies) -> type[BaseHTTPRequestHandler]:
    class BillingHandler(BaseHTTPRequestHandler):
        server_version = "UWOPlatformBilling/0.3"

        def _request_id(self) -> str:
            supplied = self.headers.get("X-Request-ID", "")
            return supplied if REQUEST_ID.fullmatch(supplied) else str(uuid.uuid4())

        def _respond(self, status: HTTPStatus, body: dict[str, Any], request_id: str) -> None:
            payload = json.dumps(_json(body), separators=(",", ":")).encode()
            self.send_response(status)
            for name, value in (
                ("Content-Type", "application/json"), ("Content-Length", str(len(payload))), ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer"),
                ("X-Request-ID", request_id),
            ):
                self.send_header(name, value)
            if status is HTTPStatus.UNAUTHORIZED:
                self.send_header("WWW-Authenticate", 'Bearer realm="uwo-platform-billing"')
            self.end_headers()
            self.wfile.write(payload)

        def _error(self, status: HTTPStatus, code: str, message: str, request_id: str) -> None:
            self._respond(status, {"error": {"code": code, "message": message}, "request_id": request_id}, request_id)

        def _body(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError as exc:
                raise InvalidRequest("invalid_request", "Content-Length must be an integer") from exc
            if length <= 0:
                raise InvalidRequest("invalid_request", "request body must not be empty")
            if length > MAX_REQUEST_BYTES:
                raise RequestTooLarge("request_too_large", f"request body must not exceed {MAX_REQUEST_BYTES} bytes")
            try:
                body = json.loads(self.rfile.read(length))
            except json.JSONDecodeError as exc:
                raise InvalidRequest("invalid_request", "request body must be valid JSON") from exc
            if not isinstance(body, dict):
                raise InvalidRequest("invalid_request", "request body must be a JSON object")
            return body

        @staticmethod
        def _fields(body: dict[str, Any], required: frozenset[str], optional: frozenset[str] = frozenset()) -> None:
            missing = required - body.keys()
            extra = body.keys() - required - optional
            if missing or extra:
                raise InvalidRequest("invalid_request", f"request fields must match the contract; missing={sorted(missing)}, extra={sorted(extra)}")

        @staticmethod
        def _integer(value: Any, name: str, minimum: int = 0) -> int:
            if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
                raise InvalidRequest("invalid_request", f"{name} must be an integer of at least {minimum}")
            return value

        def _identity(self) -> VerifiedSubjectIdentity:
            return dependencies.authenticator.authenticate(self.headers.get("Authorization", ""))

        def _route(self, method: str) -> None:
            request_id = self._request_id()
            parsed = urlsplit(self.path)
            if method == "GET" and parsed.path == "/healthz":
                self._respond(HTTPStatus.OK, {"status": "ok", "service": "platform-billing", "request_id": request_id}, request_id)
                return
            if not parsed.path.startswith("/v1/"):
                self._error(HTTPStatus.NOT_FOUND, "not_found", "route not found", request_id)
                return
            identity = None
            try:
                identity = self._identity()
                result, status = self._dispatch(method, [unquote(item) for item in parsed.path.strip("/").split("/")], parse_qs(parsed.query, keep_blank_values=True), identity, request_id)
            except AuthenticationError as exc:
                dependencies.audit.emit(audit_event("billing.administration_denied", request_id, "denied", reason_code=exc.code))
                self._error(HTTPStatus.UNAUTHORIZED, exc.code, str(exc), request_id)
                return
            except AuthorizationDenied as exc:
                dependencies.audit.emit(audit_event("billing.administration_denied", request_id, "denied", actor_subject=identity.subject if identity else None, reason_code=exc.code))
                self._error(HTTPStatus.FORBIDDEN, exc.code, str(exc), request_id)
                return
            except ResourceNotFound as exc:
                dependencies.audit.emit(audit_event("billing.administration_denied", request_id, "denied", actor_subject=identity.subject if identity else None, reason_code=exc.code))
                self._error(HTTPStatus.NOT_FOUND, exc.code, str(exc), request_id)
                return
            except PaymentRequired as exc:
                self._error(HTTPStatus.PAYMENT_REQUIRED, exc.code, str(exc), request_id)
                return
            except RequestTooLarge as exc:
                self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, exc.code, str(exc), request_id)
                return
            except Conflict as exc:
                if exc.code == "invalid_reservation_transition":
                    dependencies.audit.emit(audit_event("billing.invalid_state_transition", request_id, "denied", actor_subject=identity.subject if identity else None, reason_code=exc.code))
                self._error(HTTPStatus.CONFLICT, exc.code, str(exc), request_id)
                return
            except InvalidRequest as exc:
                self._error(HTTPStatus.BAD_REQUEST, exc.code, str(exc), request_id)
                return
            except (TypeError, ValueError):
                self._error(HTTPStatus.BAD_REQUEST, "invalid_request", "request contains invalid values", request_id)
                return
            except Exception:
                dependencies.audit.emit(audit_event("billing.internal_error", request_id, "failed", actor_subject=identity.subject if identity else None, reason_code="internal_error"))
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "an internal error occurred", request_id)
                return
            self._respond(status, {"data": _json(result), "request_id": request_id}, request_id)

        def _dispatch(self, method: str, segments: list[str], query: dict[str, list[str]], identity: VerifiedSubjectIdentity, request_id: str):
            service = dependencies.service
            key = self.headers.get("Idempotency-Key", "")
            if segments == ["v1", "billing", "accounts"] and method == "POST":
                body = self._body(); self._fields(body, frozenset({"tenant_id", "expected_version"}))
                expected_version = self._integer(body["expected_version"], "expected_version")
                result = service.create_account(identity, body["tenant_id"], key, request_id, expected_version)
                return result.account, HTTPStatus.CREATED if result.created else HTTPStatus.OK
            if len(segments) >= 4 and segments[:3] == ["v1", "billing", "accounts"]:
                tenant_id = segments[3]
                if len(segments) == 4 and method == "GET":
                    return service.read_account(identity, tenant_id, request_id), HTTPStatus.OK
                if segments[4:] == ["balance"] and method == "GET":
                    return service.read_balance(identity, tenant_id, request_id), HTTPStatus.OK
                if segments[4:] == ["status"] and method == "POST":
                    body = self._body(); self._fields(body, frozenset({"status", "expected_version"}))
                    return service.set_account_status(identity, tenant_id, BillingAccountStatus(body["status"]), self._integer(body["expected_version"], "expected_version", 1), request_id), HTTPStatus.OK
                if len(segments) == 6 and segments[4] == "credits" and method == "POST":
                    body = self._body(); self._fields(body, frozenset({"amount_microunits", "expected_version"}))
                    amount = self._integer(body["amount_microunits"], "amount_microunits", 1); version = self._integer(body["expected_version"], "expected_version", 1)
                    if segments[5] == "grants":
                        return service.grant_credits(identity, tenant_id, amount, version, key, request_id), HTTPStatus.CREATED
                    if segments[5] == "refunds":
                        return service.refund(identity, tenant_id, amount, version, key, request_id), HTTPStatus.CREATED
                if segments[4:] == ["credits", "adjustments"] and method == "POST":
                    body = self._body(); self._fields(body, frozenset({"delta_microunits", "expected_version"}))
                    return service.adjust_credits(identity, tenant_id, body["delta_microunits"], self._integer(body["expected_version"], "expected_version", 1), key, request_id), HTTPStatus.CREATED
                if segments[4:] == ["ledger"] and method == "GET":
                    if set(query) - {"limit", "cursor"}:
                        raise InvalidRequest("invalid_pagination", "unsupported pagination parameter")
                    limit = int(query.get("limit", ["50"])[0]); cursor = query.get("cursor", [None])[0]
                    page = service.list_ledger(identity, tenant_id, limit, cursor, request_id)
                    return {"items": page.items, "page": {"limit": limit, "next_cursor": page.next_cursor}}, HTTPStatus.OK
                if segments[4:] == ["rate-card"] and method == "GET":
                    return service.active_rate_card(identity, tenant_id, request_id), HTTPStatus.OK
                if len(segments) == 6 and segments[4] == "usage" and method == "GET":
                    return service.read_usage(identity, tenant_id, segments[5], request_id), HTTPStatus.OK
            if segments == ["v1", "billing", "reservations"] and method == "POST":
                body = self._body(); self._fields(body, frozenset({"tenant_id", "product", "model", "request_id", "estimated_microunits", "expires_at", "expected_balance_version"}))
                result = service.reserve(identity, body["tenant_id"], Product(body["product"]), body["model"], body["request_id"], self._integer(body["estimated_microunits"], "estimated_microunits", 1), body["expires_at"], self._integer(body["expected_balance_version"], "expected_balance_version", 1), key)
                return result, HTTPStatus.CREATED if result.created else HTTPStatus.OK
            if len(segments) == 5 and segments[:3] == ["v1", "billing", "reservations"] and method == "POST":
                reservation_id, action = segments[3], segments[4]
                body = self._body()
                if action == "capture":
                    required = frozenset({"usage_event_id", "provider_id", "provider_model_id", "region", "provider_request_id", "input_tokens", "output_tokens", "total_tokens", "expected_reservation_version", "expected_balance_version"})
                    self._fields(body, required, frozenset({"administrative_override"}))
                    dimensions = UsageDimensions(self._integer(body["input_tokens"], "input_tokens"), self._integer(body["output_tokens"], "output_tokens"), self._integer(body["total_tokens"], "total_tokens"))
                    result = service.capture(identity, reservation_id, body["usage_event_id"], body["provider_id"], body["provider_model_id"], body["region"], body["provider_request_id"], dimensions, self._integer(body["expected_reservation_version"], "expected_reservation_version", 1), self._integer(body["expected_balance_version"], "expected_balance_version", 1), key, request_id, body.get("administrative_override", False))
                    return result, HTTPStatus.CREATED if result.created else HTTPStatus.OK
                if action == "release":
                    self._fields(body, frozenset({"expected_reservation_version", "expected_balance_version"}))
                    result = service.release(identity, reservation_id, self._integer(body["expected_reservation_version"], "expected_reservation_version", 1), self._integer(body["expected_balance_version"], "expected_balance_version", 1), key, request_id)
                    return result, HTTPStatus.OK
            raise ResourceNotFound("not_found", "route not found")

        def do_GET(self) -> None: self._route("GET")  # noqa: N802
        def do_POST(self) -> None: self._route("POST")  # noqa: N802
        def log_message(self, format: str, *args: object) -> None:
            print(json.dumps({"event": "http_request", "message": format % args}))

    return BillingHandler


def create_server(dependencies: BillingDependencies, host: str = "127.0.0.1", port: int = 8091) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), _handler(dependencies))


def main() -> None:
    raise SystemExit("billing startup requires deployment-supplied authentication and durable repositories")


if __name__ == "__main__":
    main()
