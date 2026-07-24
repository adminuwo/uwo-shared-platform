import unittest

from packages.contracts import *
from services.data_service_common import AuthorizationDenied,Conflict
from control_plane_support import ADMIN_A,ADMIN_B
from phase3d_support import tenant_admin_fixture


class TenantAdministrationTests(unittest.TestCase):
    def setUp(self):self.ctx,self.state,self.clients,self.service=tenant_admin_fixture()
    def start(self,key="onboard-1"):return self.service.start_onboarding(ADMIN_A,"tenant-a","in",{"products":["aisa"],"models":["uwo-general-v1"]},key,"start")
    def complete(self,workflow):
        while workflow.status is not TenantAdministrationWorkflowStatus.COMPLETED:workflow=self.service.continue_workflow(ADMIN_A,"tenant-a",workflow.workflow_id,workflow.version,f"continue-{workflow.version}")
        return workflow
    def test_resumable_onboarding_persists_receipts_and_completes(self):
        workflow=self.complete(self.start())
        self.assertEqual(workflow.current_step,7);self.assertEqual(len(self.state.receipts),7);self.assertEqual(len(self.clients.results),7)
        self.assertEqual(len([r for r in self.state.outbox.pending() if r.event.event_type=="tenant.workflow.completed"]),1)
    def test_retry_after_dependency_failure_uses_same_external_key(self):
        self.clients.fail_once="validate-tenant-region";workflow=self.start();blocked=self.service.continue_workflow(ADMIN_A,"tenant-a",workflow.workflow_id,workflow.version,"blocked")
        self.assertEqual(blocked.status,TenantAdministrationWorkflowStatus.BLOCKED)
        recovered=self.service.continue_workflow(ADMIN_A,"tenant-a",workflow.workflow_id,blocked.version,"retry")
        calls=[item for item in self.clients.calls if item[0]=="validate-tenant-region"]
        self.assertEqual(len(calls),2);self.assertEqual(calls[0][2],calls[1][2]);self.assertEqual(len([key for key in self.clients.results if key[0]=="validate-tenant-region"]),1);self.assertEqual(recovered.current_step,1)
    def test_idempotent_start_replays_and_conflicting_reuse_fails(self):
        first=self.start();same=self.start();self.assertEqual(first,same);self.assertEqual(len(self.state.workflows),1)
        with self.assertRaises(Conflict):self.service.start_onboarding(ADMIN_A,"tenant-a","eu",{},"onboard-1","conflict")
    def test_active_claim_excludes_concurrent_worker(self):
        workflow=self.start();original=self.clients.validate_tenant
        def nested(tenant,region,key):
            current=self.state.workflows[workflow.workflow_id]
            with self.assertRaises(Conflict) as denied:self.service.continue_workflow(ADMIN_A,"tenant-a",workflow.workflow_id,current.version,"concurrent","worker-2")
            self.assertEqual(denied.exception.code,"workflow_step_claimed");return original(tenant,region,key)
        self.clients.validate_tenant=nested
        result=self.service.continue_workflow(ADMIN_A,"tenant-a",workflow.workflow_id,workflow.version,"worker-1","worker-1");self.assertEqual(result.current_step,1)
    def test_suspension_and_reactivation_coordinate_authoritative_status(self):
        workflow=self.service.start_suspension(ADMIN_A,"tenant-a","in","suspend-1","start")
        workflow=self.complete(workflow);self.assertEqual(self.clients.tenant_status,"suspended")
        react=self.service.start_reactivation(ADMIN_A,"tenant-a","in","reactivate-1","start-react")
        react=self.complete(react);self.assertEqual(self.clients.tenant_status,"active")
    def test_decommission_is_plan_only_and_preserves_evidence(self):
        before=len(self.clients.calls);plan=self.service.create_decommission_plan(ADMIN_A,"tenant-a","in","decommission")
        self.assertTrue(plan.evidence_preservation_required);self.assertEqual(len(self.clients.calls),before);self.assertIn("preserve-audit",plan.step_operations)
    def test_cancellation_and_terminal_rules(self):
        workflow=self.start();cancelled=self.service.cancel_workflow(ADMIN_A,"tenant-a",workflow.workflow_id,workflow.version,"cancel")
        self.assertEqual(cancelled.status,TenantAdministrationWorkflowStatus.CANCELLED)
        with self.assertRaises(Conflict):self.service.continue_workflow(ADMIN_A,"tenant-a",workflow.workflow_id,cancelled.version,"resume")
    def test_tenant_isolation(self):
        with self.assertRaises(AuthorizationDenied):self.service.start_onboarding(ADMIN_B,"tenant-a","in",{},"isolated","request")
    def test_operational_profile_is_provider_neutral(self):
        profile=self.service.read_operational_profile(ADMIN_A,"tenant-a");self.assertEqual((profile.region,profile.billing_status,profile.active_policy_release_id),("in","active","release-initial"))
