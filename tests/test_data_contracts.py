import unittest
from packages.contracts import *
from data_services_support import NOW

class DataServiceContractTests(unittest.TestCase):
    def test_metadata_is_deeply_immutable_and_deterministic(self):
        attrs={"resource_id":"object-1","status":"active"}
        event=DurableAuditEvent("event-1","tenant-a",1,"storage.created","succeeded",NOW,"request-1",None,attrs,"0"*64,"a"*64)
        attrs["status"]="deleted"
        self.assertEqual(event.attributes["status"],"active")
        with self.assertRaises(TypeError):event.attributes["status"]="changed"
        self.assertEqual(contract_json(event),contract_json(event))
    def test_forbidden_or_non_scalar_audit_metadata_fails(self):
        common=("event-1","tenant-a",1,"audit.test","failed",NOW,"request-1",None)
        with self.assertRaises(ValueError):DurableAuditEvent(*common,{"prompt":"secret"},"0"*64,"a"*64)
        with self.assertRaises(ValueError):DurableAuditEvent(*common,{"resource_id":{"prompt":"secret"}},"0"*64,"a"*64)
        with self.assertRaises(ValueError):DurableAuditEvent(*common,{"resource_id":1.5},"0"*64,"a"*64)
    def test_storage_integrity_contract_requires_sha2(self):
        with self.assertRaises(ValueError):ContentIntegrityMetadata("md5","a"*32)
        self.assertEqual(ContentIntegrityMetadata("sha256","a"*64).algorithm,"sha256")
    def test_analytics_dimensions_are_allowlisted(self):
        with self.assertRaises(TypeError):AnalyticsDimensions(outcome="ok",prompt="forbidden")
