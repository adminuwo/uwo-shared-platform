"""Authenticated HTTP API for Phase 3D operational services."""

from http import HTTPStatus

from services.data_service_common import InvalidRequest
from services.data_service_http import handler


def router(service):
    def route(method, parts, query, body, identity, request_id, idempotency_key):
        if method == "POST" and parts == ["v1", "operations", "services"]:
            return service.register_service_identity(identity, body["service_id"], body["component_id"], body["environment"], body["region"], request_id), HTTPStatus.CREATED
        if method == "POST" and parts == ["v1", "operations", "metrics"]:
            return service.register_metric(identity, body["tenant_id"], body["service_id"], body["metric_id"], body["name"], body["kind"], body["unit"], body.get("monotonic", False), body.get("histogram_bounds", []), request_id), HTTPStatus.CREATED
        if method == "POST" and parts == ["v1", "operations", "telemetry", "samples"]:
            return service.ingest_metric_sample(identity, body["tenant_id"], body["service_id"], body["metric_id"], body["source_sample_id"], body["observed_at"], body["value"], body.get("buckets", []), body.get("metadata", {}), request_id), HTTPStatus.ACCEPTED
        if method == "POST" and parts == ["v1", "operations", "telemetry", "dependencies"]:
            return service.ingest_dependency_health(identity, body["tenant_id"], body["service_id"], body["dependency_service_id"], body["status"], body["reason_code"], body["observed_at"], body["source_id"]), HTTPStatus.ACCEPTED
        if method == "GET" and parts == ["v1", "operations", "telemetry", "samples"]:
            return service.query_samples(identity, query["tenant_id"][0], query["metric_id"][0], query["start"][0], query["end"][0], int(query.get("limit", ["100"])[0]), query.get("cursor", [None])[0]), HTTPStatus.OK
        if method == "GET" and parts == ["v1", "operations", "dependencies"]:
            return service.list_dependencies(identity, query["tenant_id"][0], query["service_id"][0]), HTTPStatus.OK
        if method == "GET" and len(parts) == 5 and parts[:4] == ["v1", "operations", "health", "services"]:
            return service.latest_service_health(identity, query["tenant_id"][0], parts[4]), HTTPStatus.OK
        if method == "POST" and parts == ["v1", "operations", "health", "snapshots"]:
            return service.create_health_snapshot(identity, body["tenant_id"], body["service_id"], request_id), HTTPStatus.CREATED
        if method == "POST" and parts == ["v1", "operations", "telemetry", "checkpoints"]:
            return service.create_telemetry_checkpoint(identity, body["tenant_id"], body["service_id"], request_id), HTTPStatus.CREATED
        if method == "POST" and parts == ["v1", "operations", "slis"]:
            return service.create_sli(identity, body["tenant_id"], body["service_id"], body["indicator_type"], body["good_metric_id"], body["total_metric_id"], body.get("latency_threshold_ms"), request_id), HTTPStatus.CREATED
        if method == "POST" and parts == ["v1", "operations", "slos"]:
            return service.create_slo(identity, body["tenant_id"], body["service_id"], body["sli_id"], body["target_basis_points"], body["minimum_completeness_basis_points"], body["window_seconds"], request_id), HTTPStatus.CREATED
        if method == "POST" and parts == ["v1", "operations", "slo-evaluations"]:
            return service.evaluate_slo(identity, body["tenant_id"], body["slo_id"], body["window_start"], body["window_end"], request_id), HTTPStatus.CREATED
        if method == "POST" and parts == ["v1", "operations", "burn-rate-evaluations"]:
            from packages.contracts import BurnRateWindow
            short=BurnRateWindow("short",body["short_window_seconds"],body["short_threshold_microunits"]);long=BurnRateWindow("long",body["long_window_seconds"],body["long_threshold_microunits"])
            return service.evaluate_burn_rate(identity,body["tenant_id"],body["slo_id"],short,long,body["window_end"],request_id),HTTPStatus.CREATED
        if method == "GET" and len(parts) == 5 and parts[:4] == ["v1", "operations", "error-budgets", "evaluations"]:
            return service.error_budget(identity, query["tenant_id"][0], parts[4]), HTTPStatus.OK
        if method == "POST" and parts == ["v1", "operations", "alert-rules"]:
            return service.create_alert_rule(identity, body["tenant_id"], body["rule_type"], body["threshold"], request_id), HTTPStatus.CREATED
        if method == "POST" and len(parts) == 5 and parts[:3] == ["v1", "operations", "alert-rules"] and parts[4] == "status":
            return service.set_alert_rule_active(identity,body["tenant_id"],parts[3],body["active"],body["expected_version"]),HTTPStatus.OK
        if method == "POST" and parts == ["v1", "operations", "alerts", "evaluate"]:
            return service.evaluate_alert(identity, body["tenant_id"], body["rule_id"], body["evidence_reference"], body["triggered"], request_id), HTTPStatus.OK
        if method == "POST" and len(parts) == 5 and parts[:3] == ["v1", "operations", "alerts"] and parts[4] == "transition":
            return service.transition_alert(identity, body["tenant_id"], parts[3], body["status"], body["expected_version"], request_id), HTTPStatus.OK
        if method == "GET" and parts == ["v1","operations","alerts"]:
            return service.list_alerts(identity,query["tenant_id"][0],int(query.get("limit",["50"])[0]),query.get("cursor",[None])[0]),HTTPStatus.OK
        if method == "POST" and parts == ["v1", "operations", "incidents"]:
            return service.create_incident(identity, body["tenant_id"], body["severity"], body["reason_code"], body["escalation_key"], body.get("platform_scoped", False), body.get("related_alert_ids", []), request_id), HTTPStatus.CREATED
        if method == "POST" and len(parts) == 5 and parts[:3] == ["v1", "operations", "alerts"] and parts[4] == "escalate":
            return service.escalate_alert(identity, body["tenant_id"], parts[3], body["severity"], request_id), HTTPStatus.CREATED
        if method == "POST" and len(parts) == 5 and parts[:3] == ["v1", "operations", "incidents"] and parts[4] == "transition":
            return service.transition_incident(identity, body["tenant_id"], parts[3], body["status"], body["reason_code"], body["expected_version"], request_id), HTTPStatus.OK
        if method == "POST" and len(parts) == 5 and parts[:3] == ["v1","operations","incidents"] and parts[4] == "reopen":
            return service.reopen_incident(identity,body["tenant_id"],parts[3],body["severity"],body["reason_code"],request_id),HTTPStatus.CREATED
        if method == "POST" and len(parts) == 5 and parts[:3] == ["v1","operations","incidents"] and parts[4] == "assign":
            return service.assign_incident(identity,body["tenant_id"],parts[3],body["owner_subject"],body["expected_version"],request_id),HTTPStatus.OK
        if method == "GET" and parts == ["v1","operations","incidents"]:
            return service.list_incidents(identity,query["tenant_id"][0],int(query.get("limit",["50"])[0]),query.get("cursor",[None])[0]),HTTPStatus.OK
        if method == "GET" and len(parts) == 4 and parts[:3] == ["v1", "operations", "incidents"]:
            return service.incident_history(identity, query["tenant_id"][0], parts[3]), HTTPStatus.OK
        if method == "POST" and parts == ["v1", "operations", "runbooks"]:
            return service.create_runbook(identity, body["tenant_id"], body["name"], request_id), HTTPStatus.CREATED
        if method == "POST" and len(parts) == 5 and parts[:3] == ["v1", "operations", "runbooks"] and parts[4] == "versions":
            return service.append_runbook_version(identity, body["tenant_id"], parts[3], body["steps"], request_id), HTTPStatus.CREATED
        if method == "POST" and len(parts) == 5 and parts[:3] == ["v1","operations","runbooks"] and parts[4] == "activate":
            return service.activate_runbook_version(identity,body["tenant_id"],parts[3],body["version_number"],body["expected_version"]),HTTPStatus.OK
        if method == "POST" and len(parts) == 5 and parts[:3] == ["v1","operations","runbooks"] and parts[4] == "retire":
            return service.retire_runbook(identity,body["tenant_id"],parts[3],body["expected_version"]),HTTPStatus.OK
        if method == "GET" and len(parts) == 4 and parts[:3] == ["v1","operations","runbooks"]:
            return service.read_runbook(identity,query["tenant_id"][0],parts[3]),HTTPStatus.OK
        if method == "GET" and len(parts) == 5 and parts[:3] == ["v1","operations","runbook-versions"] and parts[4] == "validate":
            return service.validate_runbook_version(identity,query["tenant_id"][0],parts[3]),HTTPStatus.OK
        if method == "POST" and parts == ["v1", "operations", "runbook-executions"]:
            return service.start_runbook_execution(identity, body["tenant_id"], body["runbook_id"], body.get("incident_id"), idempotency_key, request_id), HTTPStatus.CREATED
        if method == "POST" and len(parts) == 5 and parts[:3] == ["v1", "operations", "runbook-executions"] and parts[4] == "steps":
            return service.record_runbook_step(identity, body["tenant_id"], parts[3], body["step_id"], body["outcome"], body["reason_code"], request_id), HTTPStatus.CREATED
        if method == "POST" and len(parts) == 5 and parts[:3] == ["v1", "operations", "runbook-executions"] and parts[4] == "transition":
            return service.transition_runbook_execution(identity, body["tenant_id"], parts[3], body["status"], body["expected_version"], request_id), HTTPStatus.OK
        if method == "GET" and len(parts) == 4 and parts[:3] == ["v1", "operations", "runbook-executions"]:
            return service.runbook_execution_history(identity, query["tenant_id"][0], parts[3]), HTTPStatus.OK
        if method == "POST" and parts == ["v1", "operations", "maintenance-windows"]:
            return service.create_maintenance_window(identity, body["tenant_id"], body["service_ids"], body["environment"], body["reason_code"], body["starts_at"], body["ends_at"], request_id), HTTPStatus.CREATED
        if method == "POST" and len(parts) == 5 and parts[:3] == ["v1", "operations", "maintenance-windows"] and parts[4] == "approve":
            return service.approve_maintenance_window(identity, body["tenant_id"], parts[3], body["expected_version"], request_id), HTTPStatus.OK
        if method == "POST" and len(parts) == 5 and parts[:3] == ["v1","operations","maintenance-windows"] and parts[4] == "evaluate":
            return service.evaluate_maintenance_window(identity,body["tenant_id"],parts[3],body["expected_version"],request_id),HTTPStatus.OK
        if method == "POST" and len(parts) == 5 and parts[:3] == ["v1","operations","maintenance-windows"] and parts[4] == "cancel":
            return service.cancel_maintenance_window(identity,body["tenant_id"],parts[3],body["expected_version"]),HTTPStatus.OK
        if method == "GET" and parts == ["v1", "operations", "maintenance-windows"]:
            return service.list_maintenance_windows(identity, query["tenant_id"][0]), HTTPStatus.OK
        raise InvalidRequest("unknown_route", "route not found")
    return route


def make_handler(service, authenticator, audit): return handler("platform-operations", authenticator, audit, router(service))
def main(): raise RuntimeError("platform-operations requires injected durable production repositories and exporter integrations")
