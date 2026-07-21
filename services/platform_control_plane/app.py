"""Versioned authenticated HTTP boundary for platform administration."""

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

from packages.contracts import MembershipStatus, Product, TenantStatus, VerifiedSubjectIdentity

from .audit import AuditSink, audit_event
from .auth import AuthenticationError, Authenticator
from .errors import AuthorizationDenied, Conflict, InvalidRequest, ResourceNotFound
from .service import PlatformControlPlane

REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
MAX_REQUEST_BYTES = 65_536


@dataclass(frozen=True)
class ControlPlaneDependencies:
    service: PlatformControlPlane
    authenticator: Authenticator
    audit: AuditSink


class RequestTooLarge(InvalidRequest):
    pass


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _handler(dependencies: ControlPlaneDependencies) -> type[BaseHTTPRequestHandler]:
    class ControlPlaneHandler(BaseHTTPRequestHandler):
        server_version = "UWOPlatformControlPlane/0.3"

        def _request_id(self) -> str:
            supplied = self.headers.get("X-Request-ID", "")
            return supplied if REQUEST_ID.fullmatch(supplied) else str(uuid.uuid4())

        def _respond(self, status: HTTPStatus, body: dict[str, Any], request_id: str) -> None:
            payload = json.dumps(_json_value(body), separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Request-ID", request_id)
            if status == HTTPStatus.UNAUTHORIZED:
                self.send_header("WWW-Authenticate", 'Bearer realm="uwo-platform-control-plane"')
            self.end_headers()
            self.wfile.write(payload)

        def _error(self, status: HTTPStatus, code: str, message: str, request_id: str) -> None:
            self._respond(status, {"error": {"code": code, "message": message}, "request_id": request_id}, request_id)

        def _body(self) -> dict[str, Any]:
            raw_length = self.headers.get("Content-Length", "")
            try:
                length = int(raw_length)
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
        def _fields(body: dict[str, Any], required: frozenset[str]) -> None:
            missing = required - body.keys()
            extra = body.keys() - required
            if missing or extra:
                raise InvalidRequest("invalid_request", f"request fields must exactly match; missing={sorted(missing)}, extra={sorted(extra)}")

        @staticmethod
        def _expected_version(value: Any, allow_zero: bool = False) -> int:
            minimum = 0 if allow_zero else 1
            if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
                raise InvalidRequest("invalid_version", f"expected_version must be an integer of at least {minimum}")
            return value

        def _identity(self) -> VerifiedSubjectIdentity:
            return dependencies.authenticator.authenticate(self.headers.get("Authorization", ""))

        @staticmethod
        def _enum(enum_type: Any, value: Any, field_name: str) -> Any:
            try:
                return enum_type(value)
            except (TypeError, ValueError) as exc:
                raise InvalidRequest("invalid_request", f"{field_name} is invalid") from exc

        def _route(self, method: str) -> None:
            request_id = self._request_id()
            parsed = urlsplit(self.path)
            if method == "GET" and parsed.path == "/healthz":
                self._respond(HTTPStatus.OK, {"status": "ok", "service": "platform-control-plane", "request_id": request_id}, request_id)
                return
            if not parsed.path.startswith("/v1/") and parsed.path != "/v1/tenants":
                self._error(HTTPStatus.NOT_FOUND, "not_found", "route not found", request_id)
                return
            identity: VerifiedSubjectIdentity | None = None
            try:
                identity = self._identity()
                segments = [unquote(item) for item in parsed.path.strip("/").split("/")]
                result, status = self._dispatch(method, segments, parse_qs(parsed.query, keep_blank_values=True), identity, request_id)
            except AuthenticationError as exc:
                dependencies.audit.emit(audit_event("administration.denied", request_id, "denied", reason_code=exc.code))
                self._error(HTTPStatus.UNAUTHORIZED, exc.code, str(exc), request_id)
                return
            except AuthorizationDenied as exc:
                dependencies.audit.emit(audit_event("administration.denied", request_id, "denied", actor_subject=identity.subject if identity else None, reason_code=exc.code))
                self._error(HTTPStatus.FORBIDDEN, exc.code, str(exc), request_id)
                return
            except ResourceNotFound as exc:
                dependencies.audit.emit(audit_event("administration.denied", request_id, "denied", actor_subject=identity.subject if identity else None, reason_code=exc.code))
                self._error(HTTPStatus.NOT_FOUND, exc.code, str(exc), request_id)
                return
            except RequestTooLarge as exc:
                self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, exc.code, str(exc), request_id)
                return
            except Conflict as exc:
                self._error(HTTPStatus.CONFLICT, exc.code, str(exc), request_id)
                return
            except InvalidRequest as exc:
                self._error(HTTPStatus.BAD_REQUEST, exc.code, str(exc), request_id)
                return
            except Exception:
                dependencies.audit.emit(audit_event("administration.internal_error", request_id, "failed", actor_subject=identity.subject if identity else None, reason_code="internal_error"))
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "an internal error occurred", request_id)
                return
            self._respond(status, {"data": _json_value(result), "request_id": request_id}, request_id)

        def _dispatch(
            self,
            method: str,
            segments: list[str],
            query: dict[str, list[str]],
            identity: VerifiedSubjectIdentity,
            request_id: str,
        ) -> tuple[Any, HTTPStatus]:
            service = dependencies.service
            if segments == ["v1", "tenants"]:
                if method == "POST":
                    body = self._body()
                    self._fields(body, frozenset({"tenant_id", "name", "region"}))
                    result = service.create_tenant(identity, body["tenant_id"], body["name"], body["region"], self.headers.get("Idempotency-Key", ""), request_id)
                    return result.tenant, HTTPStatus.CREATED if result.created else HTTPStatus.OK
                if method == "GET":
                    if set(query) - {"limit", "cursor"}:
                        raise InvalidRequest("invalid_pagination", "unsupported pagination parameter")
                    limit_values = query.get("limit", ["50"])
                    cursor_values = query.get("cursor", [None])
                    if len(limit_values) != 1 or len(cursor_values) != 1:
                        raise InvalidRequest("invalid_pagination", "pagination parameters must appear at most once")
                    try:
                        limit = int(limit_values[0])
                    except ValueError as exc:
                        raise InvalidRequest("invalid_pagination", "limit must be an integer") from exc
                    page = service.list_tenants(identity, limit, cursor_values[0], request_id)
                    return {"items": page.items, "page": {"limit": limit, "next_cursor": page.next_cursor}}, HTTPStatus.OK
            if len(segments) >= 3 and segments[:2] == ["v1", "tenants"]:
                tenant_id = segments[2]
                if len(segments) == 3 and method == "GET":
                    return service.get_tenant(identity, tenant_id, request_id), HTTPStatus.OK
                if segments[3:] == ["status"] and method == "POST":
                    body = self._body()
                    self._fields(body, frozenset({"status", "expected_version"}))
                    status = self._enum(TenantStatus, body["status"], "status")
                    return service.set_tenant_status(identity, tenant_id, status, self._expected_version(body["expected_version"]), request_id), HTTPStatus.OK
                if len(segments) == 5 and segments[3] == "memberships" and method == "PUT":
                    body = self._body()
                    self._fields(body, frozenset({"status", "expected_version"}))
                    status = self._enum(MembershipStatus, body["status"], "status")
                    result = service.put_membership(identity, tenant_id, segments[4], status, self._expected_version(body["expected_version"], allow_zero=True), request_id)
                    return result.membership, HTTPStatus.CREATED if result.created else HTTPStatus.OK
                if len(segments) == 7 and segments[3] == "memberships" and segments[5] == "roles":
                    body = self._body()
                    self._fields(body, frozenset({"expected_version"}))
                    version = self._expected_version(body["expected_version"])
                    if method == "POST":
                        return service.assign_role(identity, tenant_id, segments[4], segments[6], version, request_id), HTTPStatus.CREATED
                    if method == "DELETE":
                        return service.revoke_role(identity, tenant_id, segments[4], segments[6], version, request_id), HTTPStatus.OK
                if len(segments) == 5 and segments[3] == "permissions" and method == "GET":
                    permissions = service.effective_permissions(identity, tenant_id, segments[4], request_id)
                    return {"tenant_id": tenant_id, "subject": segments[4], "permissions": permissions}, HTTPStatus.OK
                if segments[3:] == ["entitlements"] and method == "GET":
                    return service.effective_entitlements(identity, tenant_id, request_id), HTTPStatus.OK
                if len(segments) == 6 and segments[3] == "entitlements" and segments[4] in {"products", "models"}:
                    body = self._body()
                    self._fields(body, frozenset({"expected_version"}))
                    version = self._expected_version(body["expected_version"])
                    resource = segments[5]
                    if segments[4] == "products":
                        product = self._enum(Product, resource, "product")
                        if method == "POST":
                            result = service.grant_product(identity, tenant_id, product, version, self.headers.get("Idempotency-Key", ""), request_id)
                            return result.entitlement, HTTPStatus.CREATED if result.created else HTTPStatus.OK
                        if method == "DELETE":
                            return service.revoke_product(identity, tenant_id, product, version, request_id), HTTPStatus.OK
                    if segments[4] == "models":
                        if method == "POST":
                            result = service.grant_model(identity, tenant_id, resource, version, self.headers.get("Idempotency-Key", ""), request_id)
                            return result.entitlement, HTTPStatus.CREATED if result.created else HTTPStatus.OK
                        if method == "DELETE":
                            return service.revoke_model(identity, tenant_id, resource, version, request_id), HTTPStatus.OK
                if segments[3:] == ["policy-version"] and method == "GET":
                    return service.policy_version(identity, tenant_id, request_id), HTTPStatus.OK
            raise ResourceNotFound("not_found", "route not found")

        def do_GET(self) -> None:  # noqa: N802
            self._route("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._route("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._route("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._route("DELETE")

        def log_message(self, format: str, *args: object) -> None:
            print(json.dumps({"event": "http_request", "message": format % args}))

    return ControlPlaneHandler


def create_server(dependencies: ControlPlaneDependencies, host: str = "127.0.0.1", port: int = 8090) -> ThreadingHTTPServer:
    """Create a server with deployment-supplied authentication and repositories."""

    return ThreadingHTTPServer((host, port), _handler(dependencies))


def main() -> None:
    raise SystemExit("control-plane startup requires deployment-supplied authentication and durable repositories")


if __name__ == "__main__":
    main()
