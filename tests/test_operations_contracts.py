import hashlib
import unittest

from packages.contracts import *

from phase3d_support import PHASE3D_NOW


class OperationsContractTests(unittest.TestCase):
    def test_policy_content_is_deeply_immutable_and_deterministic(self):
        source={"z":{"items":[2,1]},"a":True}
        draft=PolicyDraft("draft-1","tenant-a","admin-a",PolicyDraftStatus.DRAFT,"uwo-policy-v1",source,None,(),PHASE3D_NOW,PHASE3D_NOW,1)
        source["z"]["items"].append(3)
        self.assertEqual(operations_json(draft.content),'{"a":true,"z":{"items":[2,1]}}')
        with self.assertRaises(TypeError):draft.content["a"]=False
    def test_nested_sensitive_policy_fields_are_rejected(self):
        with self.assertRaises(ValueError):PolicyDraft("draft-1","tenant-a","admin-a",PolicyDraftStatus.DRAFT,"uwo-policy-v1",{"safe":{"api_key":"x"}},None,(),PHASE3D_NOW,PHASE3D_NOW,1)
    def test_release_digest_binds_canonical_content(self):
        content={"region":"in"};digest=ConfigurationDigest("sha256",hashlib.sha256(operations_json(content).encode()).hexdigest())
        release=PolicyRelease("release-1","tenant-a","change-1","uwo-policy-v1",content,digest,None,"admin-a",PHASE3D_NOW)
        self.assertEqual(release.digest.digest,hashlib.sha256(operations_json(release.content).encode()).hexdigest())
    def test_telemetry_metadata_is_scalar_and_allowlisted(self):
        with self.assertRaises(ValueError):MetricSample("sample-1","metric-1","service-1",PHASE3D_NOW,1,(),{"prompt":"secret"})
        with self.assertRaises(ValueError):MetricSample("sample-1","metric-1","service-1",PHASE3D_NOW,1,(),{"operation":{"nested":1}})
    def test_histogram_buckets_and_integer_values_are_enforced(self):
        with self.assertRaises(ValueError):OperationalMetric("metric-1","service-1","latency",MetricKind.HISTOGRAM,"ms",False,(100,50),PHASE3D_NOW,1)
        with self.assertRaises(ValueError):MetricSample("sample-1","metric-1","service-1",PHASE3D_NOW,-1,(),{})
    def test_slo_uses_integer_basis_points_only(self):
        self.assertEqual(SLOTarget(9990,9000).target_basis_points,9990)
        with self.assertRaises(ValueError):SLOTarget(99.9,9000)
    def test_runbook_rejects_executable_content(self):
        with self.assertRaises(ValueError):RunbookStep("step-1","runbook-1",1,1,RunbookStepType.MANUAL_CHECK,"kubectl delete pod")
    def test_production_maintenance_requires_distinct_approval(self):
        with self.assertRaises(ValueError):MaintenanceWindow("mw-1","tenant-a",("service-1",),PolicyEnvironment.PRODUCTION,MaintenanceWindowStatus.APPROVED,"planned","admin-a","admin-a",PHASE3D_NOW,"2026-07-24T07:00:00+00:00",PHASE3D_NOW,1)
    def test_error_budget_never_becomes_negative(self):
        budget=ErrorBudget("budget-1","evaluation-1","slo-1",1,5,0,PHASE3D_NOW);self.assertEqual(budget.remaining_bad_events,0)
