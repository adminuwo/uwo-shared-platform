"""Authenticated HTTP boundary for policy governance."""

from http import HTTPStatus
from services.data_service_common import InvalidRequest
from services.data_service_http import handler


def router(service):
    def route(method, parts, query, body, identity, request_id, idempotency_key):
        if method == "POST" and parts == ["v1", "governance", "drafts"]:
            return service.create_draft(identity, body["tenant_id"], body["content"], body["compatibility_version"], body.get("base_release_id"), body.get("risk_categories", []), request_id), HTTPStatus.CREATED
        if len(parts) == 4 and parts[:3] == ["v1", "governance", "drafts"] and method == "PUT":
            return service.update_draft(identity, body["tenant_id"], parts[3], body["content"], body.get("risk_categories", []), body["expected_version"], request_id), HTTPStatus.OK
        if len(parts) == 5 and parts[:3] == ["v1", "governance", "drafts"] and method == "POST":
            if parts[4] == "validate": return service.validate_draft(identity, body["tenant_id"], parts[3], request_id), HTTPStatus.OK
            if parts[4] == "submit": return service.submit_change(identity, body["tenant_id"], parts[3], request_id), HTTPStatus.CREATED
        if len(parts) == 5 and parts[:3] == ["v1", "governance", "changes"] and parts[4] == "decisions" and method == "POST":
            return service.decide_change(identity, body["tenant_id"], parts[3], body["decision"], body["reason_code"], request_id), HTTPStatus.CREATED
        if len(parts) == 5 and parts[:3] == ["v1", "governance", "changes"] and parts[4] == "release" and method == "POST":
            return service.create_release(identity, body["tenant_id"], parts[3], request_id), HTTPStatus.CREATED
        if method == "POST" and parts == ["v1", "governance", "promotions"]:
            return service.promote(identity, body["tenant_id"], body["release_id"], body["environment"], body["expected_environment_version"], idempotency_key, request_id), HTTPStatus.CREATED
        if method == "POST" and parts == ["v1", "governance", "rollbacks"]:
            return service.rollback(identity, body["tenant_id"], body["environment"], body["target_release_id"], body["expected_environment_version"], idempotency_key, request_id), HTTPStatus.CREATED
        if method == "GET" and parts == ["v1", "governance", "active-release"]:
            return service.active_release(identity, query["tenant_id"][0], query["environment"][0]), HTTPStatus.OK
        if method == "GET" and parts == ["v1", "governance", "compare"]:
            return service.compare_releases(identity, query["tenant_id"][0], query["left"][0], query["right"][0]), HTTPStatus.OK
        if method == "GET" and parts == ["v1", "governance", "history"]:
            return service.history(identity, query["tenant_id"][0]), HTTPStatus.OK
        raise InvalidRequest("unknown_route", "route not found")
    return route


def make_handler(service, authenticator, audit): return handler("platform-governance", authenticator, audit, router(service))
def main(): raise RuntimeError("platform-governance requires injected durable production repositories and identity integrations")
