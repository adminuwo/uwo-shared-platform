import unittest
from datetime import datetime,timedelta,timezone
from packages.contracts import *
from services.data_service_common import AuthorizationDenied,Conflict,PolicyViolation
from services.platform_analytics.in_memory import InMemoryAnalyticsState,InMemoryAnalyticsUnitOfWorkFactory
from services.platform_analytics.service import PlatformAnalyticsService
from control_plane_support import ADMIN_A,ADMIN_B,PLATFORM,NOW
from data_services_support import make_data_context

class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.now=NOW;self.ctx=make_data_context();self.state=InMemoryAnalyticsState();self.service=PlatformAnalyticsService(InMemoryAnalyticsUnitOfWorkFactory(self.state),self.ctx.authorizer,self.ctx.audit,minimum_export_count=2,clock=lambda:self.now)
    def advance(self,seconds):self.now=(datetime.fromisoformat(self.now.replace("Z","+00:00"))+timedelta(seconds=seconds)).astimezone(timezone.utc).isoformat()
    def event(self,event_id="event-1",occurred=NOW,tenant_id="tenant-a",recorded=NOW):return AnalyticsEvent(event_id,tenant_id,Product.AISA,"in",AnalyticsEventType.REQUEST_COMPLETED,AnalyticsDimensions("succeeded","under-1s","under-1k","under-1-credit"),occurred,recorded)
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
    def test_service_owns_recorded_at_and_rejects_future_events(self):
        original=self.event(recorded="2025-01-01T00:00:00+00:00");value=self.service.ingest(ADMIN_A,"tenant-a",original,"recorded","recorded");self.assertEqual(value.recorded_at,NOW);self.advance(1);replay=self.service.ingest(ADMIN_A,"tenant-a",self.event(recorded="2024-01-01T00:00:00+00:00"),"recorded","retry");self.assertEqual(replay,value)
        with self.assertRaises(PolicyViolation) as denied:self.service.ingest(ADMIN_A,"tenant-a",self.event("future","2026-07-20T12:01:02+00:00"),"future","future")
        self.assertEqual(denied.exception.code,"future_event_rejected")
    def test_window_is_half_open_and_metric_ids_include_boundaries(self):
        self.service.ingest(ADMIN_A,"tenant-a",self.event("at-start","2026-07-20T11:00:00Z"),"start","start");self.service.ingest(ADMIN_A,"tenant-a",self.event("at-end","2026-07-20T12:00:00+00:00"),"end","end")
        first=self.service.aggregate(ADMIN_A,"tenant-a",AggregationWindow("2026-07-20T11:00:00+00:00","2026-07-20T12:00:00Z"));second=self.service.aggregate(ADMIN_A,"tenant-a",AggregationWindow("2026-07-20T12:00:00Z","2026-07-20T13:00:00+00:00"))
        self.assertEqual((first[0].count,second[0].count),(1,1));self.assertNotEqual(first[0].metric_id,second[0].metric_id)
    def test_cross_tenant_export_suppresses_low_counts_and_returns_points_only(self):
        window=AggregationWindow("2026-07-20T11:00:00+00:00","2026-07-20T13:00:00+00:00")
        self.service.ingest(ADMIN_A,"tenant-a",self.event("tenant-a-one"),"a1","a1");a=self.service.create_snapshot(ADMIN_A,"tenant-a",window,"snapshot-a")
        self.service.ingest(ADMIN_B,"tenant-b",self.event("tenant-b-one",tenant_id="tenant-b"),"b1","b1");self.service.ingest(ADMIN_B,"tenant-b",self.event("tenant-b-two",tenant_id="tenant-b"),"b2","b2");b=self.service.create_snapshot(ADMIN_B,"tenant-b",window,"snapshot-b")
        result=self.service.cross_tenant_export(PLATFORM,(a.snapshot_id,b.snapshot_id));self.assertEqual(len(result),1);self.assertIsInstance(result[0],MetricPoint);self.assertEqual((result[0].tenant_id,result[0].count),("tenant-b",2));self.assertNotIsInstance(result[0],AnalyticsSnapshot)
