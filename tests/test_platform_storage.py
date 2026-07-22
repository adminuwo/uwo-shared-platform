import unittest
from packages.contracts import *
from services.data_service_common import AuthorizationDenied,PolicyViolation,RepositoryIntegrityError
from services.platform_storage.in_memory import FakeBlobStore,InMemoryStorageRepository,InMemoryStorageUnitOfWorkFactory
from services.platform_storage.service import PlatformStorageService
from control_plane_support import ADMIN_A,ADMIN_B,PLATFORM,NOW
from data_services_support import make_data_context

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.ctx=make_data_context();self.state=InMemoryStorageRepository();self.blob=FakeBlobStore();self.service=PlatformStorageService(InMemoryStorageUnitOfWorkFactory(self.state),self.blob,self.ctx.authorizer,self.ctx.audit,allowed_regions=frozenset({"in"}),clock=lambda:NOW);self.integrity=ContentIntegrityMetadata("sha256","a"*64)
    def initiate(self,key="upload-key",classification=ObjectClassification.INTERNAL,retain_until=None):return self.service.initiate_upload(ADMIN_A,"tenant-a",Product.AISA,"in",classification,10,self.integrity,key,"request-upload",retain_until=retain_until)
    def finalize(self,session,key="final-key"):
        self.blob.stage(session.storage_key,10,"sha256","a"*64);return self.service.finalize_upload(ADMIN_A,"tenant-a",session.upload_id,key,"request-final")
    def test_upload_finalize_idempotency_and_version_immutability(self):
        session=self.initiate();result=self.finalize(session);replay=self.service.finalize_upload(ADMIN_A,"tenant-a",session.upload_id,"final-key","retry")
        self.assertEqual(result,replay);self.assertEqual(len(self.state.versions),1);self.assertEqual(result.object.current_version,1)
        session2=self.service.initiate_upload(ADMIN_A,"tenant-a",Product.AISA,"in",ObjectClassification.INTERNAL,10,self.integrity,"u2","request-u2",object_id=result.object.object_id);result2=self.finalize(session2,"f2")
        self.assertEqual((result.object.current_version,result2.object.current_version),(1,2));self.assertEqual(len(self.state.versions),2)
    def test_checksum_mismatch_rolls_back(self):
        s=self.initiate();self.blob.stage(s.storage_key,10,"sha256","b"*64)
        with self.assertRaises(PolicyViolation):self.service.finalize_upload(ADMIN_A,"tenant-a",s.upload_id,"f","req")
        self.assertFalse(self.state.objects);self.assertFalse(self.state.versions)
    def test_tenant_isolation_and_region_policy(self):
        s=self.initiate();result=self.finalize(s)
        with self.assertRaises(AuthorizationDenied):self.service.get_object(ADMIN_B,"tenant-a",result.object.object_id)
        with self.assertRaises(PolicyViolation):self.service.initiate_upload(ADMIN_A,"tenant-a",Product.AISA,"us",ObjectClassification.INTERNAL,1,self.integrity,"bad-region","req")
    def test_retention_legal_hold_deleted_and_malware_download_denials(self):
        s=self.initiate(classification=ObjectClassification.RESTRICTED,retain_until="2027-01-01T00:00:00+00:00");r=self.finalize(s)
        with self.assertRaises(PolicyViolation):self.service.mark_deleted(ADMIN_A,"tenant-a",r.object.object_id,1,"delete")
        self.service.set_legal_hold(ADMIN_A,"tenant-a",r.object.object_id,True,"investigation",0,"hold")
        with self.assertRaises(PolicyViolation):self.service.mark_deleted(ADMIN_A,"tenant-a",r.object.object_id,1,"delete")
        with self.assertRaises(PolicyViolation):self.service.authorize_download(ADMIN_A,"tenant-a",r.object.object_id,"download")
        self.service.record_malware_scan(PLATFORM,"tenant-a",r.object_version.object_version_id,MalwareScanStatus.INFECTED,"scan")
        with self.assertRaises(PolicyViolation):self.service.authorize_download(ADMIN_A,"tenant-a",r.object.object_id,"download")
    def test_clean_object_has_opaque_timed_download_authorization(self):
        r=self.finalize(self.initiate());self.service.record_malware_scan(PLATFORM,"tenant-a",r.object_version.object_version_id,MalwareScanStatus.CLEAN,"scan");auth=self.service.authorize_download(ADMIN_A,"tenant-a",r.object.object_id,"download")
        self.assertTrue(auth.opaque_token.startswith("opaque-"));self.assertNotIn(r.object_version.storage_key,auth.opaque_token)
    def test_deleted_object_and_missing_restricted_policy_fail_closed(self):
        with self.assertRaises(PolicyViolation):self.initiate("restricted-missing",ObjectClassification.RESTRICTED)
        r=self.finalize(self.initiate("deletable"));self.service.record_malware_scan(PLATFORM,"tenant-a",r.object_version.object_version_id,MalwareScanStatus.CLEAN,"scan");self.service.mark_deleted(ADMIN_A,"tenant-a",r.object.object_id,1,"delete")
        with self.assertRaises(PolicyViolation):self.service.authorize_download(ADMIN_A,"tenant-a",r.object.object_id,"download")
    def test_transaction_failure_leaves_no_partial_state(self):
        self.state.fail_next="idempotency"
        with self.assertRaises(RepositoryIntegrityError):self.initiate()
        self.assertFalse(self.state.uploads);self.assertFalse(self.state.idempotency);self.assertFalse(self.state.outbox.pending())
