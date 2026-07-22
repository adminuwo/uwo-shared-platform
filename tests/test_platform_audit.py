import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from packages.contracts import *
from services.data_service_common import AuthorizationDenied
from services.platform_audit.in_memory import InMemoryAuditState,InMemoryAuditUnitOfWorkFactory
from services.platform_audit.service import PlatformAuditService
from control_plane_support import ADMIN_A,ADMIN_B,PLATFORM,NOW
from data_services_support import make_data_context

class AuditTests(unittest.TestCase):
    def setUp(self):
        self.ctx=make_data_context();self.state=InMemoryAuditState();self.service=PlatformAuditService(InMemoryAuditUnitOfWorkFactory(self.state),self.ctx.authorizer,self.ctx.audit,clock=lambda:NOW)
    def append(self,n):return self.service.append(PLATFORM,"tenant-a","storage.object-created","succeeded",f"request-{n}",{"resource_id":f"object-{n}","region":"in"})
    def test_monotonic_hash_chain_checkpoint_and_export(self):
        first=self.append(1);second=self.append(2);self.assertEqual((first.sequence,second.sequence),(1,2));self.assertEqual(second.previous_hash,first.current_hash);self.assertTrue(self.service.verify(ADMIN_A,"tenant-a").valid);checkpoint=self.service.checkpoint(ADMIN_A,"tenant-a","cp");self.assertTrue(self.service.verify_checkpoint(ADMIN_A,"tenant-a",checkpoint.checkpoint_id));manifest,events=self.service.export(ADMIN_A,"tenant-a","export");self.assertEqual((manifest.event_count,len(events)),(2,2))
    def test_tamper_detection_redaction_pagination_and_isolation(self):
        self.append(1);self.append(2);self.state.events["tenant-a"][0]=replace(self.state.events["tenant-a"][0],outcome="failed");proof=self.service.verify(ADMIN_A,"tenant-a");self.assertFalse(proof.valid);self.assertEqual(proof.first_invalid_sequence,1)
        page=self.service.list(ADMIN_A,"tenant-a",1);self.assertEqual(len(page.items),1);self.assertIsNotNone(page.next_cursor);self.assertTrue(page.items[0].redacted)
        with self.assertRaises(AuthorizationDenied):self.service.list(ADMIN_B,"tenant-a")
    def test_concurrent_appends_allocate_unique_sequences(self):
        with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(self.append,range(20)))
        sequences=[event.sequence for event in self.state.events["tenant-a"]];self.assertEqual(sequences,list(range(1,21)));self.assertTrue(self.service.verify(ADMIN_A,"tenant-a").valid)
    def test_retention_and_legal_hold_metadata(self):
        value=self.service.set_retention(PLATFORM,"tenant-a","2027-01-01T00:00:00+00:00",True,None,"retention");self.assertTrue(value.legal_hold);self.assertEqual(value.version,1)
