import unittest
from dataclasses import replace

from packages.contracts import *
from services.data_service_common import AuthorizationDenied,Conflict,PolicyViolation
from control_plane_support import ADMIN_A,ADMIN_B,PLATFORM,USER_A
from phase3d_support import PHASE3D_NOW,operations_fixture

LATER="2026-07-24T06:00:01+00:00"
WINDOW_START="2026-07-24T05:00:00+00:00"
WINDOW_END="2026-07-24T07:00:00+00:00"


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.ctx,self.state,self.exporter,self.service=operations_fixture()
        self.service.register_service_identity(PLATFORM,"service-a","component-a","development","in","register")
        self.service.register_service_identity(PLATFORM,"dependency-a","dependency","development","in","register-dependency")
    def metric(self,metric_id="requests-total",kind="counter",monotonic=True,bounds=()):
        return self.service.register_metric(ADMIN_A,"tenant-a","service-a",metric_id,metric_id,kind,"count",monotonic,bounds,f"metric-{metric_id}")
    def sample(self,metric_id,source,value,observed=PHASE3D_NOW,buckets=(),metadata=None):
        return self.service.ingest_metric_sample(PLATFORM,"tenant-a","service-a",metric_id,source,observed,value,buckets,metadata or {"tenant_id":"tenant-a","operation":"request"},"ingest")
    def setup_slo(self,target=9950,indicator="availability"):
        self.metric("good","gauge",False);self.metric("total","gauge",False)
        sli=self.service.create_sli(ADMIN_A,"tenant-a","service-a",indicator,"good","total",None,"sli")
        return self.service.create_slo(ADMIN_A,"tenant-a","service-a",sli.sli_id,target,9000,7200,"slo")
    def test_allowlisted_idempotent_ingestion_and_export(self):
        self.metric();first=self.sample("requests-total","source-1",10);same=self.sample("requests-total","source-1",10)
        self.assertEqual(first,same);self.assertEqual(len(self.state.metric_samples),1);self.assertEqual(len(self.exporter.samples),1)
    def test_conflicting_sample_and_sensitive_metadata_fail_closed(self):
        self.metric();self.sample("requests-total","source-1",10)
        with self.assertRaises(Conflict):self.sample("requests-total","source-1",11)
        with self.assertRaises(ValueError):self.sample("requests-total","source-2",12,metadata={"prompt":"sensitive"})
    def test_monotonic_counter_and_histogram_rules(self):
        self.metric();self.sample("requests-total","source-1",10)
        with self.assertRaises(PolicyViolation):self.sample("requests-total","source-2",9,LATER)
        self.metric("latency","histogram",False,(100,500));result=self.sample("latency","hist-1",4,buckets=({"upper_bound":100,"count":2},{"upper_bound":500,"count":4}))
        self.assertEqual(len(result.buckets),2)
        with self.assertRaises(PolicyViolation):self.sample("latency","hist-2",2,buckets=({"upper_bound":50,"count":2},{"upper_bound":500,"count":2}))
    def test_future_and_late_samples_are_rejected(self):
        self.metric()
        with self.assertRaises(PolicyViolation):self.sample("requests-total","old",1,"2026-07-22T00:00:00+00:00")
        with self.assertRaises(PolicyViolation):self.sample("requests-total","future",1,"2026-07-24T07:00:00+00:00")
    def test_missing_health_is_unknown_and_export_failure_is_isolated(self):
        unknown=self.service.latest_service_health(ADMIN_A,"tenant-a","service-a");self.assertEqual(unknown.status,ServiceHealthStatus.UNKNOWN)
        self.metric();self.sample("requests-total","source-1",1);self.exporter.fail_next=True
        healthy=self.service.create_health_snapshot(ADMIN_A,"tenant-a","service-a","snapshot");self.assertEqual(healthy.status,ServiceHealthStatus.HEALTHY);self.assertFalse(self.exporter.health)
    def test_dependency_unavailable_makes_service_unhealthy(self):
        self.metric();self.sample("requests-total","source-1",1)
        self.service.ingest_dependency_health(PLATFORM,"tenant-a","service-a","dependency-a","unhealthy","dependency-down",PHASE3D_NOW,"dep-1")
        snapshot=self.service.create_health_snapshot(ADMIN_A,"tenant-a","service-a","snapshot");self.assertEqual(snapshot.status,ServiceHealthStatus.UNHEALTHY)
    def test_tenant_isolation_for_telemetry_reads(self):
        self.metric();self.sample("requests-total","source-1",1)
        with self.assertRaises(AuthorizationDenied):self.service.query_samples(ADMIN_B,"tenant-a","requests-total",WINDOW_START,WINDOW_END)
    def test_checkpoint_is_canonical_and_requires_samples(self):
        with self.assertRaises(PolicyViolation):self.service.create_telemetry_checkpoint(ADMIN_A,"tenant-a","service-a","empty")
        self.metric();self.sample("requests-total","source-1",1)
        checkpoint=self.service.create_telemetry_checkpoint(ADMIN_A,"tenant-a","service-a","checkpoint");self.assertEqual(checkpoint.sample_count,1);self.assertEqual(checkpoint.digest.algorithm,"sha256")
    def test_deterministic_slo_evaluation_and_immutable_history(self):
        slo=self.setup_slo();self.service.create_alert_rule(ADMIN_A,"tenant-a","slo-breach",1,"rule");self.sample("good","good-1",99);self.sample("total","total-1",100)
        first=self.service.evaluate_slo(ADMIN_A,"tenant-a",slo.slo_id,WINDOW_START,WINDOW_END,"eval")
        second=self.service.evaluate_slo(ADMIN_A,"tenant-a",slo.slo_id,WINDOW_START,WINDOW_END,"eval-retry")
        self.assertEqual(first,second);self.assertEqual(first.achieved_basis_points,9900);self.assertEqual(first.state,SLOEvaluationState.BREACHED);self.assertEqual(len(self.state.slo_evaluations),1);self.assertEqual(len(self.state.alert_occurrences),1)
    def test_missing_slo_data_returns_unknown(self):
        slo=self.setup_slo();value=self.service.evaluate_slo(ADMIN_A,"tenant-a",slo.slo_id,WINDOW_START,WINDOW_END,"missing")
        self.assertEqual(value.state,SLOEvaluationState.UNKNOWN);self.assertIsNone(value.achieved_basis_points)
        self.sample("total","total-only",100);partial=self.service.evaluate_slo(ADMIN_A,"tenant-a",slo.slo_id,WINDOW_START,"2026-07-24T07:00:01+00:00","partial");self.assertEqual(partial.state,SLOEvaluationState.UNKNOWN)
    def test_error_budget_and_burn_rate_use_integer_fixed_point(self):
        slo=self.setup_slo(9900);self.sample("good","good-1",95);self.sample("total","total-1",100)
        evaluation=self.service.evaluate_slo(ADMIN_A,"tenant-a",slo.slo_id,WINDOW_START,WINDOW_END,"eval");budget=self.service.error_budget(ADMIN_A,"tenant-a",evaluation.evaluation_id)
        self.assertEqual(budget.remaining_bad_events,0)
        burn=self.service.evaluate_burn_rate(ADMIN_A,"tenant-a",slo.slo_id,BurnRateWindow("short",3600,1_000_000),BurnRateWindow("long",7200,1_000_000),WINDOW_END,"burn")
        self.assertIsInstance(burn.short_rate_microunits,int);self.assertTrue(burn.breached)
    def activate_maintenance(self,reason="planned"):
        window=self.service.create_maintenance_window(ADMIN_A,"tenant-a",("service-a",),"production",reason,WINDOW_START,WINDOW_END,"maintenance")
        approved=self.service.approve_maintenance_window(USER_A,"tenant-a",window.maintenance_window_id,window.version,"approve")
        return self.service.evaluate_maintenance_window(USER_A,"tenant-a",window.maintenance_window_id,approved.version,"activate")
    def test_maintenance_exclusion_remains_visible_in_slo_evidence(self):
        window=self.activate_maintenance();slo=self.setup_slo();self.sample("good","good-1",99);self.sample("total","total-1",100)
        evaluation=self.service.evaluate_slo(ADMIN_A,"tenant-a",slo.slo_id,WINDOW_START,WINDOW_END,"eval")
        self.assertEqual(evaluation.state,SLOEvaluationState.UNKNOWN);self.assertEqual(evaluation.excluded_maintenance_window_ids,(window.maintenance_window_id,))
    def test_maintenance_never_suppresses_audit_integrity_failure(self):
        self.activate_maintenance();rule=self.service.create_alert_rule(ADMIN_A,"tenant-a","audit-integrity-failure",1,"rule")
        alert=self.service.evaluate_alert(ADMIN_A,"tenant-a",rule.rule_id,"audit-checkpoint-1",True,"evaluate");self.assertEqual(alert.status,AlertStatus.OPEN)
    def test_alert_deduplication_suppression_acknowledgement_and_resolution(self):
        self.activate_maintenance();rule=self.service.create_alert_rule(ADMIN_A,"tenant-a","slo-breach",1,"rule")
        alert=self.service.evaluate_alert(ADMIN_A,"tenant-a",rule.rule_id,"evaluation-1",True,"evaluate");same=self.service.evaluate_alert(ADMIN_A,"tenant-a",rule.rule_id,"evaluation-1",True,"retry")
        self.assertEqual(alert,same);self.assertEqual(alert.status,AlertStatus.SUPPRESSED);self.assertEqual(alert.suppression_reason_code,"active-maintenance-window")
        rule2=self.service.create_alert_rule(ADMIN_A,"tenant-a","billing-compensation-failure-threshold",1,"rule2")
        open_alert=self.service.evaluate_alert(ADMIN_A,"tenant-a",rule2.rule_id,"billing-event-1",True,"open")
        # Existing active maintenance suppresses this rule too, so end it before lifecycle validation.
        window=next(iter(self.state.maintenance_windows.values()));self.state.maintenance_windows[window.maintenance_window_id]=replace(window,status=MaintenanceWindowStatus.EXPIRED,version=window.version+1)
        open_alert=self.service.evaluate_alert(ADMIN_A,"tenant-a",rule2.rule_id,"billing-event-2",True,"open-2")
        acknowledged=self.service.transition_alert(ADMIN_A,"tenant-a",open_alert.alert_id,"acknowledged",open_alert.version,"ack")
        resolved=self.service.transition_alert(ADMIN_A,"tenant-a",open_alert.alert_id,"resolved",acknowledged.version,"resolve");self.assertEqual(resolved.status,AlertStatus.RESOLVED)
    def test_alert_escalation_creates_exactly_one_incident(self):
        rule=self.service.create_alert_rule(ADMIN_A,"tenant-a","service-unhealthy",1,"rule");alert=self.service.evaluate_alert(ADMIN_A,"tenant-a",rule.rule_id,"health-1",True,"open")
        first=self.service.escalate_alert(ADMIN_A,"tenant-a",alert.alert_id,"sev2","escalate");second=self.service.escalate_alert(ADMIN_A,"tenant-a",alert.alert_id,"sev2","retry")
        self.assertEqual(first,second);self.assertEqual(len(self.state.incidents),1)
    def test_incident_lifecycle_and_immutable_timeline(self):
        incident=self.service.create_incident(ADMIN_A,"tenant-a","sev2","service-down","manual-1",False,(),"open")
        for target in ("acknowledged","mitigating","resolved","closed"):
            incident=self.service.transition_incident(ADMIN_A,"tenant-a",incident.incident_id,target,target,incident.version,target)
        history=self.service.incident_history(ADMIN_A,"tenant-a",incident.incident_id);self.assertEqual(incident.status,IncidentStatus.CLOSED);self.assertEqual(len(history["timeline"]),5)
        with self.assertRaises(PolicyViolation):self.service.transition_incident(ADMIN_A,"tenant-a",incident.incident_id,"acknowledged","reopen",incident.version,"reopen")
        reopened=self.service.reopen_incident(ADMIN_A,"tenant-a",incident.incident_id,"sev3","regression","new-incident");self.assertNotEqual(reopened.incident_id,incident.incident_id);self.assertEqual(reopened.parent_incident_id,incident.incident_id)
    def setup_runbook(self):
        runbook=self.service.create_runbook(ADMIN_A,"tenant-a","Provider outage","create")
        version=self.service.append_runbook_version(ADMIN_A,"tenant-a",runbook.runbook_id,({"step_type":"manual_check","instruction":"Check the approved service health dashboard"},{"step_type":"verification","instruction":"Verify that the stable health state recovered"}),"version")
        active=self.service.activate_runbook_version(ADMIN_A,"tenant-a",runbook.runbook_id,1,runbook.version)
        return active,version
    def test_runbook_version_binding_order_pause_resume_and_terminal_rules(self):
        runbook,version=self.setup_runbook();execution=self.service.start_runbook_execution(ADMIN_A,"tenant-a",runbook.runbook_id,None,"execute-1","start")
        self.assertEqual(execution.runbook_version_id,version.runbook_version_id);self.assertTrue(self.service.validate_runbook_version(ADMIN_A,"tenant-a",version.runbook_version_id)["valid"])
        with self.assertRaises(PolicyViolation):self.service.record_runbook_step(ADMIN_A,"tenant-a",execution.execution_id,version.steps[1].step_id,"ok","verified","bad-order")
        first=self.service.record_runbook_step(ADMIN_A,"tenant-a",execution.execution_id,version.steps[0].step_id,"ok","checked","step-1");self.assertEqual(first,self.service.record_runbook_step(ADMIN_A,"tenant-a",execution.execution_id,version.steps[0].step_id,"ok","checked","step-1-retry"))
        current=self.state.runbook_executions[execution.execution_id];paused=self.service.transition_runbook_execution(ADMIN_A,"tenant-a",execution.execution_id,"paused",current.version,"pause")
        resumed=self.service.transition_runbook_execution(ADMIN_A,"tenant-a",execution.execution_id,"running",paused.version,"resume")
        self.service.record_runbook_step(ADMIN_A,"tenant-a",execution.execution_id,version.steps[1].step_id,"ok","verified","step-2")
        current=self.state.runbook_executions[execution.execution_id];completed=self.service.transition_runbook_execution(ADMIN_A,"tenant-a",execution.execution_id,"completed",current.version,"complete")
        self.assertEqual(completed.status,RunbookExecutionStatus.COMPLETED);self.assertEqual(len(self.state.runbook_step_results),2)
        with self.assertRaises(PolicyViolation):self.service.transition_runbook_execution(ADMIN_A,"tenant-a",execution.execution_id,"paused",completed.version,"mutate")
    def test_runbook_start_is_idempotent_and_links_incident(self):
        runbook,_=self.setup_runbook();incident=self.service.create_incident(ADMIN_A,"tenant-a","sev3","provider","manual-2",False,(),"incident")
        first=self.service.start_runbook_execution(ADMIN_A,"tenant-a",runbook.runbook_id,incident.incident_id,"execute-2","start");same=self.service.start_runbook_execution(ADMIN_A,"tenant-a",runbook.runbook_id,incident.incident_id,"execute-2","retry")
        self.assertEqual(first,same);self.assertEqual(first.incident_id,incident.incident_id);self.assertEqual(len(self.state.runbook_executions),1)
    def test_maintenance_duration_self_approval_and_cancellation(self):
        with self.assertRaises(PolicyViolation):self.service.create_maintenance_window(ADMIN_A,"tenant-a",("service-a",),"development","too-long",WINDOW_START,"2026-08-24T07:00:00+00:00","long")
        window=self.service.create_maintenance_window(ADMIN_A,"tenant-a",("service-a",),"production","planned",WINDOW_START,WINDOW_END,"create")
        with self.assertRaises(PolicyViolation):self.service.approve_maintenance_window(ADMIN_A,"tenant-a",window.maintenance_window_id,window.version,"self")
        cancelled=self.service.cancel_maintenance_window(ADMIN_A,"tenant-a",window.maintenance_window_id,window.version);self.assertEqual(cancelled.status,MaintenanceWindowStatus.CANCELLED)
