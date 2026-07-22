import unittest
from packages.contracts import TenantStatus
from services.data_service_common import CollectingEventPublisher
from services.platform_billing.errors import PaymentRequired
from billing_support import EXECUTOR,make_billing_fixture,provision
from control_plane_support import PLATFORM,make_fixture

class CrossServiceEventTests(unittest.TestCase):
    def test_control_plane_status_and_billing_low_balance_publish_once(self):
        control_events=CollectingEventPublisher();control=make_fixture(control_events);control.service.create_tenant(PLATFORM,"tenant-event","Event Tenant","in","create","create-request");control.service.set_tenant_status(PLATFORM,"tenant-event",TenantStatus.SUSPENDED,1,"status-request")
        status=[event for event in control_events.events if event.event_type=="tenant.status-changed"]
        self.assertEqual(len(status),1);self.assertEqual(status[0].attributes["status"],"suspended")
        billing_events=CollectingEventPublisher();billing=make_billing_fixture(event_publisher=billing_events);provision(billing)
        with self.assertRaises(PaymentRequired):billing.service.authorize_estimated_charge(EXECUTOR,"tenant-billing",1,"balance-request")
        low=[event for event in billing_events.events if event.event_type=="billing.balance-low"]
        self.assertEqual(len(low),1);self.assertEqual(low[0].attributes["reason_code"],"insufficient_balance")
