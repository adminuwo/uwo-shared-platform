import unittest
from unittest.mock import patch
from packages.contracts import TenantStatus
from services.data_service_common import CollectingEventPublisher,OutboxDispatcher,OutboxStatus
from services.platform_billing.errors import PaymentRequired
from billing_support import EXECUTOR,make_billing_fixture,provision
from control_plane_support import NOW,PLATFORM,make_fixture

class FailingRecorder:
    def record(self,event):raise RuntimeError("event infrastructure unavailable")
    def publish(self,event):raise RuntimeError("event infrastructure unavailable")

class CrossServiceEventTests(unittest.TestCase):
    def test_control_plane_status_and_billing_low_balance_publish_once(self):
        control_events=CollectingEventPublisher();control=make_fixture();control.service.create_tenant(PLATFORM,"tenant-event","Event Tenant","in","create","create-request");control.service.set_tenant_status(PLATFORM,"tenant-event",TenantStatus.SUSPENDED,1,"status-request");record=control.outbox.pending()[0];OutboxDispatcher(control.outbox,control_events,"control-worker").dispatch(record.record_id,NOW,"2026-07-20T12:00:31+00:00")
        status=[event for event in control_events.events if event.event_type=="tenant.status-changed"]
        self.assertEqual(len(status),1);self.assertEqual(status[0].attributes["status"],"suspended")
        billing_events=CollectingEventPublisher();billing=make_billing_fixture(event_recorder=billing_events);provision(billing)
        with self.assertRaises(PaymentRequired):billing.service.authorize_estimated_charge(EXECUTOR,"tenant-billing",1,"balance-request")
        low=[event for event in billing_events.events if event.event_type=="billing.balance-low"]
        self.assertEqual(len(low),1);self.assertEqual(low[0].attributes["reason_code"],"insufficient_balance")
    def test_event_failure_does_not_change_committed_status_or_billing_denial(self):
        control=make_fixture();control.service.create_tenant(PLATFORM,"tenant-event-failure","Event Tenant","in","create","create-request");changed=control.service.set_tenant_status(PLATFORM,"tenant-event-failure",TenantStatus.SUSPENDED,1,"status-request");record=control.outbox.pending()[0];retry=OutboxDispatcher(control.outbox,FailingRecorder(),"control-worker").dispatch(record.record_id,NOW,"2026-07-20T12:00:31+00:00");self.assertEqual(retry.status,OutboxStatus.PENDING);self.assertEqual(changed.status,TenantStatus.SUSPENDED);self.assertEqual(control.tenants.get("tenant-event-failure").status,TenantStatus.SUSPENDED)
        billing=make_billing_fixture(event_recorder=FailingRecorder());provision(billing)
        with self.assertRaises(PaymentRequired):billing.service.authorize_estimated_charge(EXECUTOR,"tenant-billing",1,"balance-request")
    def test_control_plane_status_and_outbox_enqueue_are_atomic(self):
        control=make_fixture();control.service.create_tenant(PLATFORM,"tenant-atomic","Atomic Tenant","in","create","create-request")
        with patch.object(control.outbox,"enqueue",side_effect=RuntimeError("outbox write failed")):
            with self.assertRaises(RuntimeError):control.service.set_tenant_status(PLATFORM,"tenant-atomic",TenantStatus.SUSPENDED,1,"status-request")
        self.assertEqual(control.tenants.get("tenant-atomic").status,TenantStatus.ACTIVE);self.assertFalse(control.outbox.pending())
