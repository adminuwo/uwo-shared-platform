import unittest
from packages.contracts import *
from services.data_service_common import AuthorizationDenied,PolicyViolation,RepositoryIntegrityError
from services.platform_notifications.in_memory import *
from services.platform_notifications.repositories import ProviderAcceptance
from services.platform_notifications.service import PlatformNotificationService
from control_plane_support import ADMIN_A,ADMIN_B,PLATFORM,NOW
from data_services_support import make_data_context

class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.ctx=make_data_context();self.state=InMemoryNotificationState();self.provider=FakeNotificationProvider();providers={c:self.provider for c in NotificationChannel};self.service=PlatformNotificationService(InMemoryNotificationUnitOfWorkFactory(self.state),self.ctx.authorizer,self.ctx.audit,providers,StaticWebhookAllowlist(frozenset({("tenant-a","hook-approved")})),clock=lambda:NOW)
        self.service.register_template(ADMIN_A,"tenant-a","template-1",Product.AISA,"in",NotificationChannel.EMAIL,"content-ref",(),"register");self.service.activate_template(ADMIN_A,"tenant-a","template-1",1,1,"activate")
    def create(self,key="dedup-1",channel=NotificationChannel.EMAIL,recipient="subject-ref"):return self.service.create_notification(ADMIN_A,"tenant-a",Product.AISA,"in","template-1",channel,recipient,key,"create")
    def test_template_versioning_enqueue_delivery_and_duplicate_prevention(self):
        v=self.service.add_template_version(ADMIN_A,"tenant-a","template-1",NotificationChannel.EMAIL,"content-v2",(),2,"add-version");self.assertEqual(v.version_number,2)
        n=self.create();same=self.create();self.assertEqual(n,same);result=self.service.dispatch(PLATFORM,"tenant-a",n.notification_id,"dispatch");self.assertEqual(result.status,NotificationStatus.DELIVERED);self.service.dispatch(PLATFORM,"tenant-a",n.notification_id,"retry");self.assertEqual(len(self.provider.calls),1)
    def test_retry_then_dead_letter_and_backoff(self):
        self.provider._outcomes=[ProviderAcceptance(False,True,None,"temporary"),ProviderAcceptance(False,False,None,"rejected")];n=self.create();first=self.service.dispatch(PLATFORM,"tenant-a",n.notification_id,"d1");self.assertEqual(first.status,NotificationStatus.ENQUEUED);self.assertEqual(self.service.retry_delay(1),30);second=self.service.dispatch(PLATFORM,"tenant-a",n.notification_id,"d2");self.assertEqual(second.status,NotificationStatus.DEAD_LETTERED);self.assertEqual(len(self.state.dead_letters),1)
    def test_provider_failure_recovers_on_bounded_retry(self):
        self.provider._outcomes=[ProviderAcceptance(False,True,None,"temporary"),ProviderAcceptance(True,False,"accepted-2",None)];n=self.create();self.service.dispatch(PLATFORM,"tenant-a",n.notification_id,"d1");result=self.service.dispatch(PLATFORM,"tenant-a",n.notification_id,"d2");self.assertEqual(result.status,NotificationStatus.DELIVERED);self.assertEqual(len(self.provider.calls),2)
    def test_cancel_optout_webhook_allowlist_and_isolation(self):
        n=self.create();cancelled=self.service.cancel(ADMIN_A,"tenant-a",n.notification_id,n.version,"cancel");self.assertEqual(cancelled.status,NotificationStatus.CANCELLED)
        self.service.set_preference(ADMIN_A,"tenant-a","opted-out",NotificationChannel.EMAIL,False,None,"pref");self.assertEqual(self.create("dedup-2",recipient="opted-out").status,NotificationStatus.SUPPRESSED)
        self.service.register_template(ADMIN_A,"tenant-a","webhook-template",Product.AISA,"in",NotificationChannel.WEBHOOK,"webhook-content",(),"register-hook");self.service.activate_template(ADMIN_A,"tenant-a","webhook-template",1,1,"activate-hook")
        with self.assertRaises(PolicyViolation):self.service.create_notification(ADMIN_A,"tenant-a",Product.AISA,"in","webhook-template",NotificationChannel.WEBHOOK,"not-allowed","dedup-hook","create-hook")
        with self.assertRaises(AuthorizationDenied):self.service.get(ADMIN_B,"tenant-a",n.notification_id)
    def test_outbox_failure_rolls_back_notification(self):
        self.state.fail_next="outbox"
        with self.assertRaises(RepositoryIntegrityError):self.create()
        self.assertFalse(self.state.notifications);self.assertFalse(self.state.outbox.pending())
