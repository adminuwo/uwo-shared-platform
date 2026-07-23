import unittest
from datetime import datetime,timedelta,timezone
from packages.contracts import *
from services.data_service_common import AuthorizationDenied,InvalidRequest,PolicyViolation,RepositoryIntegrityError
from services.platform_storage.in_memory import FakeBlobStore,InMemoryStorageRepository,InMemoryStorageUnitOfWorkFactory
from services.platform_storage.service import PlatformStorageService
from control_plane_support import ADMIN_A,ADMIN_B,PLATFORM,NOW
from data_services_support import make_data_context

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.now=NOW;self.ctx=make_data_context();self.state=InMemoryStorageRepository();self.blob=FakeBlobStore();self.service=PlatformStorageService(InMemoryStorageUnitOfWorkFactory(self.state),self.blob,self.ctx.authorizer,self.ctx.audit,allowed_regions=frozenset({"in"}),clock=lambda:self.now);self.integrity=ContentIntegrityMetadata("sha256","a"*64)
    def advance(self,seconds):self.now=(datetime.fromisoformat(self.now.replace("Z","+00:00"))+timedelta(seconds=seconds)).astimezone(timezone.utc).isoformat()
    def initiate(self,key="upload-key",classification=ObjectClassification.INTERNAL,retain_until=None):return self.service.initiate_upload(ADMIN_A,"tenant-a",Product.AISA,"in",classification,10,self.integrity,key,f"request-{key}",retain_until=retain_until)
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
        self.service.record_malware_scan(PLATFORM,"tenant-a",r.object_version.object_version_id,MalwareScanStatus.INFECTED,"source-infected","scan")
        with self.assertRaises(PolicyViolation):self.service.authorize_download(ADMIN_A,"tenant-a",r.object.object_id,"download")
    def test_clean_object_has_opaque_timed_download_authorization(self):
        r=self.finalize(self.initiate());self.service.record_malware_scan(PLATFORM,"tenant-a",r.object_version.object_version_id,MalwareScanStatus.CLEAN,"source-clean","scan");auth=self.service.authorize_download(ADMIN_A,"tenant-a",r.object.object_id,"download")
        self.assertTrue(auth.opaque_token.startswith("opaque-"));self.assertNotIn(r.object_version.storage_key,auth.opaque_token)
    def test_deleted_object_and_missing_restricted_policy_fail_closed(self):
        with self.assertRaises(PolicyViolation):self.initiate("restricted-missing",ObjectClassification.RESTRICTED)
        r=self.finalize(self.initiate("deletable"));self.service.record_malware_scan(PLATFORM,"tenant-a",r.object_version.object_version_id,MalwareScanStatus.CLEAN,"source-deletable","scan");self.service.mark_deleted(ADMIN_A,"tenant-a",r.object.object_id,1,"delete")
        with self.assertRaises(PolicyViolation):self.service.authorize_download(ADMIN_A,"tenant-a",r.object.object_id,"download")
    def test_transaction_failure_leaves_no_partial_state(self):
        self.state.fail_next="idempotency"
        with self.assertRaises(RepositoryIntegrityError):self.initiate()
        self.assertFalse(self.state.uploads);self.assertFalse(self.state.idempotency);self.assertFalse(self.state.outbox.pending())
    def test_expired_upload_cannot_be_finalized(self):
        session=self.initiate();self.blob.stage(session.storage_key,10,"sha256","a"*64);self.advance(3601)
        with self.assertRaises(PolicyViolation) as denied:self.service.finalize_upload(ADMIN_A,"tenant-a",session.upload_id,"expired","expired")
        self.assertEqual(denied.exception.code,"upload_expired");self.assertFalse(self.state.objects);self.assertFalse(self.state.versions)
    def test_retention_shortening_requires_audited_platform_override(self):
        result=self.finalize(self.initiate());first=self.service.apply_retention(ADMIN_A,"tenant-a",result.object.object_id,"2027-07-20T12:00:00+00:00",0,"retention")
        with self.assertRaises(PolicyViolation) as denied:self.service.apply_retention(ADMIN_A,"tenant-a",result.object.object_id,"2027-01-01T00:00:00+00:00",first.version,"shorten")
        self.assertEqual(denied.exception.code,"retention_shortening_denied")
        changed=self.service.apply_retention(PLATFORM,"tenant-a",result.object.object_id,"2027-01-01T00:00:00+00:00",first.version,"override",override=True,reason_code="legal-approval")
        self.assertEqual(changed.version,2);events=[event for event in self.ctx.audit.events if event.event_type=="storage.retention_overridden"];self.assertEqual(len(events),1);self.assertEqual(events[0].reason_code,"legal-approval")
    def test_malware_scan_history_does_not_mutate_object_version(self):
        result=self.finalize(self.initiate());original=self.state.versions[result.object_version.object_version_id]
        first=self.service.record_malware_scan(PLATFORM,"tenant-a",original.object_version_id,MalwareScanStatus.INFECTED,"source-history-1","scan-1");self.advance(1);second=self.service.record_malware_scan(PLATFORM,"tenant-a",original.object_version_id,MalwareScanStatus.CLEAN,"source-history-2","scan-2")
        self.assertEqual(self.state.versions[original.object_version_id],original);self.assertNotEqual(first.scan_result_id,second.scan_result_id);self.assertEqual(len(self.state.scans),2);self.assertEqual(self.service.current_scan_status(ADMIN_A,"tenant-a",original.object_version_id),MalwareScanStatus.CLEAN)
    def test_deleted_object_cannot_receive_a_new_version(self):
        result=self.finalize(self.initiate());self.service.mark_deleted(ADMIN_A,"tenant-a",result.object.object_id,result.object.version,"delete")
        session=self.service.initiate_upload(ADMIN_A,"tenant-a",Product.AISA,"in",ObjectClassification.INTERNAL,10,self.integrity,"new-version","new-version",object_id=result.object.object_id);self.blob.stage(session.storage_key,10,"sha256","a"*64)
        with self.assertRaises(PolicyViolation) as denied:self.service.finalize_upload(ADMIN_A,"tenant-a",session.upload_id,"new-final","new-final")
        self.assertEqual(denied.exception.code,"object_deleted");self.assertEqual(len(self.state.versions),1)
    def test_utc_equivalent_retention_deadline_and_download_ttl_bounds(self):
        result=self.finalize(self.initiate());self.service.apply_retention(ADMIN_A,"tenant-a",result.object.object_id,"2026-07-20T12:00:00Z",0,"retention");deleted=self.service.mark_deleted(ADMIN_A,"tenant-a",result.object.object_id,result.object.version,"delete");self.assertEqual(deleted.status,ObjectStatus.DELETED)
        clean=self.finalize(self.initiate("ttl-object"),"ttl-final");self.service.record_malware_scan(PLATFORM,"tenant-a",clean.object_version.object_version_id,MalwareScanStatus.CLEAN,"source-ttl","scan")
        for ttl in (0,29,3601):
            with self.assertRaises(InvalidRequest):self.service.authorize_download(ADMIN_A,"tenant-a",clean.object.object_id,"download",ttl)
    def test_malware_scan_exact_replay_has_one_history_entry_and_event(self):
        result=self.finalize(self.initiate("scan-replay"));first=self.service.record_malware_scan(PLATFORM,"tenant-a",result.object_version.object_version_id,MalwareScanStatus.INFECTED,"scanner-job-1","scan-first");self.advance(10)
        replay=self.service.record_malware_scan(PLATFORM,"tenant-a",result.object_version.object_version_id,MalwareScanStatus.INFECTED,"scanner-job-1","scan-retry")
        self.assertEqual(replay,first);self.assertEqual(len(self.state.scans),1);self.assertEqual(len(self.state.outbox.pending()),3)
        malware=[x for x in self.state.outbox.pending() if x.event.event_type=="storage.object.malware-detected"];self.assertEqual(len(malware),1)
    def test_malware_scan_source_conflicts_fail_closed_across_object_tenant_and_status(self):
        first=self.finalize(self.initiate("scan-conflict-1"));second=self.finalize(self.initiate("scan-conflict-2"),"scan-conflict-final-2")
        self.service.record_malware_scan(PLATFORM,"tenant-a",first.object_version.object_version_id,MalwareScanStatus.CLEAN,"scanner-job-conflict","scan")
        cases=(("tenant-a",second.object_version.object_version_id,MalwareScanStatus.CLEAN),("tenant-b",first.object_version.object_version_id,MalwareScanStatus.CLEAN),("tenant-a",first.object_version.object_version_id,MalwareScanStatus.INFECTED))
        for tenant,version,status in cases:
            with self.assertRaises(Exception) as denied:self.service.record_malware_scan(PLATFORM,tenant,version,status,"scanner-job-conflict","retry")
            self.assertEqual(denied.exception.code,"idempotency_conflict")
        self.assertEqual(len(self.state.scans),1)
    def test_malware_scan_and_event_roll_back_atomically(self):
        result=self.finalize(self.initiate("scan-rollback"));before_outbox=len(self.state.outbox.pending());self.state.fail_next="outbox"
        with self.assertRaises(RepositoryIntegrityError):self.service.record_malware_scan(PLATFORM,"tenant-a",result.object_version.object_version_id,MalwareScanStatus.INFECTED,"scanner-job-rollback","scan")
        self.assertFalse(self.state.scans);self.assertEqual(len(self.state.outbox.pending()),before_outbox);self.assertFalse(any(key[1]=="scanner-job-rollback" for key in self.state.idempotency))
