import unittest
from packages.contracts import *
from services.data_service_common import AuthorizationDenied,Conflict,PolicyViolation
from services.platform_analytics.in_memory import InMemoryAnalyticsState,InMemoryAnalyticsUnitOfWorkFactory
from services.platform_analytics.service import PlatformAnalyticsService
from control_plane_support import ADMIN_A,ADMIN_B,NOW
from data_services_support import make_data_context

class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.ctx=make_data_context();self.state=InMemoryAnalyticsState();self.service=PlatformAnalyticsService(InMemoryAnalyticsUnitOfWorkFactory(self.state),self.ctx.authorizer,self.ctx.audit,minimum_export_count=2,clock=lambda:NOW)
    def event(self,event_id="event-1",occurred=NOW):return AnalyticsEvent(event_id,"tenant-a",Product.AISA,"in",AnalyticsEventType.REQUEST_COMPLETED,AnalyticsDimensions("succeeded","under-1s","under-1k","under-1-credit"),occurred,NOW)
    def test_allowlisted_idempotent_ingestion_and_no_double_count(self):
        event=self.event();self.service.ingest(ADMIN_A,"tenant-a",event,"idem","request");same=self.service.ingest(ADMIN_A,"tenant-a",event,"idem","retry");self.assertEqual(event,same);window=AggregationWindow("2026-07-20T11:00:00+00:00","2026-07-20T13:00:00+00:00");self.assertEqual(self.service.aggregate(ADMIN_A,"tenant-a",window)[0].count,1)
        with self.assertRaises(Conflict):self.service.ingest(ADMIN_A,"tenant-a",self.event("event-2"),"idem","conflict")
    def test_window_boundaries_late_events_and_minimum_threshold(self):
        with self.assertRaises(PolicyViolation):self.service.ingest(ADMIN_A,"tenant-a",self.event("late","2026-07-18T00:00:00+00:00"),"late","late")
        self.service.ingest(ADMIN_A,"tenant-a",self.event(),"i1","r1");window=AggregationWindow("2026-07-20T12:00:00+00:00","2026-07-20T13:00:00+00:00");snapshot=self.service.create_snapshot(ADMIN_A,"tenant-a",window,"snap");self.assertEqual(self.service.export_snapshot(ADMIN_A,"tenant-a",snapshot.snapshot_id),())
        self.service.ingest(ADMIN_A,"tenant-a",self.event("event-2"),"i2","r2");snapshot2=self.service.create_snapshot(ADMIN_A,"tenant-a",window,"snap2");self.assertEqual(self.service.export_snapshot(ADMIN_A,"tenant-a",snapshot2.snapshot_id)[0].count,2);self.assertEqual(snapshot2.integrity_hash,self.service.create_snapshot(ADMIN_A,"tenant-a",window,"snap3").integrity_hash)
    def test_tenant_and_cross_tenant_isolation(self):
        self.service.ingest(ADMIN_A,"tenant-a",self.event(),"i","r")
        with self.assertRaises(AuthorizationDenied):self.service.get(ADMIN_B,"tenant-a","event-1")
        with self.assertRaises(AuthorizationDenied):self.service.cross_tenant_export(ADMIN_A,())
