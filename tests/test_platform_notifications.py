import unittest
from datetime import datetime, timedelta, timezone
from packages.contracts import *
from services.data_service_common import AuthorizationDenied,Conflict,InfrastructureUnavailable,OutboxStatus,PolicyViolation,RepositoryIntegrityError,deterministic_id
from services.platform_notifications.in_memory import *
from services.platform_notifications.repositories import ProviderAcceptance
from services.platform_notifications.service import PlatformNotificationService
from control_plane_support import ADMIN_A,ADMIN_B,PLATFORM,NOW
from data_services_support import make_data_context

class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.now=NOW;self.ctx=make_data_context();self.state=InMemoryNotificationState();self.provider=FakeNotificationProvider();providers={c:self.provider for c in NotificationChannel};self.service=PlatformNotificationService(InMemoryNotificationUnitOfWorkFactory(self.state),self.ctx.authorizer,self.ctx.audit,providers,StaticWebhookAllowlist(frozenset({("tenant-a","hook-approved")})),clock=lambda:self.now)
        self.service.register_template(ADMIN_A,"tenant-a","template-1",Product.AISA,"in",NotificationChannel.EMAIL,"content-ref",(),"register");self.service.activate_template(ADMIN_A,"tenant-a","template-1",1,1,"activate")
    def advance(self,seconds):
        value=datetime.fromisoformat(self.now.replace("Z","+00:00"))+timedelta(seconds=seconds);self.now=value.astimezone(timezone.utc).isoformat()
    def create(self,key="dedup-1",channel=NotificationChannel.EMAIL,recipient="subject-ref"):return self.service.create_notification(ADMIN_A,"tenant-a",Product.AISA,"in","template-1",channel,recipient,key,"create")
    def test_template_versioning_enqueue_delivery_and_duplicate_prevention(self):
        v=self.service.add_template_version(ADMIN_A,"tenant-a","template-1",NotificationChannel.EMAIL,"content-v2",(),2,"add-version");self.assertEqual(v.version_number,2)
        n=self.create();same=self.create();self.assertEqual(n,same);result=self.service.dispatch(PLATFORM,"tenant-a",n.notification_id,"dispatch");self.assertEqual(result.status,NotificationStatus.DELIVERED);self.service.dispatch(PLATFORM,"tenant-a",n.notification_id,"retry");self.assertEqual(len(self.provider.calls),1)
    def test_retry_then_dead_letter_and_backoff(self):
        self.provider._outcomes=[ProviderAcceptance(False,True,None,"temporary"),ProviderAcceptance(False,False,None,"rejected")];n=self.create();first=self.service.dispatch(PLATFORM,"tenant-a",n.notification_id,"d1");self.assertEqual(first.status,NotificationStatus.ENQUEUED);self.assertEqual(self.service.retry_delay(1),30)
        with self.assertRaises(Conflict) as denied:self.service.dispatch(PLATFORM,"tenant-a",n.notification_id,"too-early")
        self.assertEqual(denied.exception.code,"retry_not_due");self.advance(30);second=self.service.dispatch(PLATFORM,"tenant-a",n.notification_id,"d2");self.assertEqual(second.status,NotificationStatus.DEAD_LETTERED);self.assertEqual(len(self.state.dead_letters),1)
    def test_provider_failure_recovers_on_bounded_retry(self):
        self.provider._outcomes=[ProviderAcceptance(False,True,None,"temporary"),ProviderAcceptance(True,False,"accepted-2",None)];n=self.create();self.service.dispatch(PLATFORM,"tenant-a",n.notification_id,"d1");self.advance(30);result=self.service.dispatch(PLATFORM,"tenant-a",n.notification_id,"d2");self.assertEqual(result.status,NotificationStatus.DELIVERED);self.assertEqual(len(self.provider.calls),2)
    def test_cancel_optout_webhook_allowlist_and_isolation(self):
        n=self.create();cancelled=self.service.cancel(ADMIN_A,"tenant-a",n.notification_id,n.version,"cancel");self.assertEqual(cancelled.status,NotificationStatus.CANCELLED);self.assertEqual(self.state.outbox.get(deterministic_id("outbox",n.notification_id)).status,OutboxStatus.CANCELLED)
        self.service.set_preference(ADMIN_A,"tenant-a","opted-out",NotificationChannel.EMAIL,False,None,"pref");self.assertEqual(self.create("dedup-2",recipient="opted-out").status,NotificationStatus.SUPPRESSED)
        self.service.register_template(ADMIN_A,"tenant-a","webhook-template",Product.AISA,"in",NotificationChannel.WEBHOOK,"webhook-content",(),"register-hook");self.service.activate_template(ADMIN_A,"tenant-a","webhook-template",1,1,"activate-hook")
        with self.assertRaises(PolicyViolation):self.service.create_notification(ADMIN_A,"tenant-a",Product.AISA,"in","webhook-template",NotificationChannel.WEBHOOK,"not-allowed","dedup-hook","create-hook")
        with self.assertRaises(AuthorizationDenied):self.service.get(ADMIN_B,"tenant-a",n.notification_id)
    def test_outbox_failure_rolls_back_notification(self):
        self.state.fail_next="outbox"
        with self.assertRaises(RepositoryIntegrityError):self.create()
        self.assertFalse(self.state.notifications);self.assertFalse(self.state.outbox.pending())
    def test_provider_acceptance_then_repository_failure_recovers_without_duplicate_delivery(self):
        n=self.create();self.state.fail_next="attempt"
        with self.assertRaises(RepositoryIntegrityError):self.service.dispatch(PLATFORM,"tenant-a",n.notification_id,"first")
        record=self.state.outbox.get(deterministic_id("outbox",n.notification_id));self.assertEqual(record.status,OutboxStatus.CLAIMED);self.assertEqual(len(self.provider.external_deliveries),1)
        self.advance(31);result=self.service.dispatch(PLATFORM,"tenant-a",n.notification_id,"recovery");self.assertEqual(result.status,NotificationStatus.DELIVERED);self.assertEqual(len(self.provider.external_deliveries),1);self.assertEqual(len(self.provider.calls),2)
    def test_conflicting_deduplication_reuse_fails_closed(self):
        self.create()
        with self.assertRaises(Conflict) as denied:self.create(recipient="different-subject")
        self.assertEqual(denied.exception.code,"deduplication_conflict")
    def test_multiple_inactive_versions_use_maximum_existing_number(self):
        second=self.service.add_template_version(ADMIN_A,"tenant-a","template-1",NotificationChannel.EMAIL,"content-v2",(),2,"v2")
        third=self.service.add_template_version(ADMIN_A,"tenant-a","template-1",NotificationChannel.EMAIL,"content-v3",(),3,"v3")
        self.assertEqual((second.version_number,third.version_number),(2,3));self.assertEqual(self.state.templates["template-1"].active_version,1)
    def test_expired_claimant_cannot_finalize_a_recovered_lease(self):
        n=self.create();_,_,stale=self.service._claim("tenant-a",n.notification_id);self.advance(31);_,_,recovered=self.service._claim("tenant-a",n.notification_id)
        accepted=ProviderAcceptance(True,False,"provider-accepted",None)
        with self.assertRaises(Conflict) as denied:self.service._finalize("tenant-a",n.notification_id,stale,accepted)
        self.assertEqual(denied.exception.code,"stale_version");self.assertFalse(self.state.attempts);self.assertEqual(self.service._finalize("tenant-a",n.notification_id,recovered,accepted).status,NotificationStatus.DELIVERED)
    def test_missing_provider_is_a_stable_infrastructure_failure(self):
        n=self.create();self.service._providers={}
        with self.assertRaises(InfrastructureUnavailable) as unavailable:self.service.dispatch(PLATFORM,"tenant-a",n.notification_id,"dispatch")
        self.assertEqual(unavailable.exception.code,"provider_not_configured");self.assertEqual(self.state.notifications[n.notification_id].status,NotificationStatus.DEAD_LETTERED);self.assertEqual(self.state.outbox.get(deterministic_id("outbox",n.notification_id)).status,OutboxStatus.DEAD_LETTERED)
    def test_final_attempt_crash_is_reconciled_without_another_delivery(self):
        self.service._max=2;self.provider._outcomes=[ProviderAcceptance(False,True,None,"temporary")]
        n=self.create();self.service.dispatch(PLATFORM,"tenant-a",n.notification_id,"first");self.advance(30)
        _,_,final_claim=self.service._claim("tenant-a",n.notification_id);self.assertEqual(final_claim.attempts,2)
        self.advance(31);recovered=self.service.dispatch(PLATFORM,"tenant-a",n.notification_id,"recover")
        self.assertEqual(recovered.status,NotificationStatus.DEAD_LETTERED);self.assertEqual(len(self.provider.calls),1)
        attempts=self.state.attempts.values();self.assertEqual(sorted(x.attempt_number for x in attempts),[1,2]);self.assertEqual(len(self.state.dead_letters),1)
        record=self.state.outbox.get(deterministic_id("outbox",n.notification_id));self.assertEqual(record.status,OutboxStatus.DEAD_LETTERED)
        replay=self.service.dispatch(PLATFORM,"tenant-a",n.notification_id,"recover-again");self.assertEqual(replay.status,NotificationStatus.DEAD_LETTERED);self.assertEqual(len(self.provider.calls),1);self.assertEqual(len(self.state.attempts),2);self.assertEqual(len(self.state.dead_letters),1)
