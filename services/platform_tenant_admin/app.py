"""Authenticated HTTP boundary for Phase 3D tenant administration."""

from http import HTTPStatus

from services.data_service_common import InvalidRequest
from services.data_service_http import handler


def router(service):
    def route(method, parts, query, body, identity, request_id, idempotency_key):
        if method == "POST" and parts == ["v1", "tenant-administration", "onboarding"]:
            return service.start_onboarding(identity, body["tenant_id"], body["region"], body.get("metadata", {}), idempotency_key, request_id), HTTPStatus.CREATED
        if method == "POST" and parts == ["v1", "tenant-administration", "suspensions"]:
            return service.start_suspension(identity, body["tenant_id"], body["region"], idempotency_key, request_id), HTTPStatus.CREATED
        if method == "POST" and parts == ["v1", "tenant-administration", "reactivations"]:
            return service.start_reactivation(identity, body["tenant_id"], body["region"], idempotency_key, request_id), HTTPStatus.CREATED
        if method == "POST" and parts == ["v1", "tenant-administration", "decommission-plans"]:
            return service.create_decommission_plan(identity, body["tenant_id"], body["region"], request_id), HTTPStatus.CREATED
        if len(parts) == 4 and parts[:2] == ["v1", "tenant-administration"] and parts[2] == "workflows":
            if method == "GET": return service.get_workflow(identity, query["tenant_id"][0], parts[3]), HTTPStatus.OK
            if method == "POST": return service.continue_workflow(identity, body["tenant_id"], parts[3], body["expected_version"], request_id, body.get("worker_id", "tenant-admin-worker")), HTTPStatus.OK
        if len(parts) == 5 and parts[:3] == ["v1", "tenant-administration", "workflows"] and parts[4] == "cancel" and method == "POST":
            return service.cancel_workflow(identity, body["tenant_id"], parts[3], body["expected_version"], request_id), HTTPStatus.OK
        if method == "GET" and parts == ["v1", "tenant-administration", "workflows"]:
            return service.list_workflows(identity, query["tenant_id"][0], int(query.get("limit", ["50"])[0]), query.get("cursor", [None])[0]), HTTPStatus.OK
        if method == "GET" and len(parts) == 4 and parts[:3] == ["v1", "tenant-administration", "profiles"]:
            return service.read_operational_profile(identity, parts[3]), HTTPStatus.OK
        raise InvalidRequest("unknown_route", "route not found")
    return route


def make_handler(service, authenticator, audit): return handler("platform-tenant-admin", authenticator, audit, router(service))
def main(): raise RuntimeError("platform-tenant-admin requires injected durable production repositories and service clients")
