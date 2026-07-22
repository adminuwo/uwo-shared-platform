"""Privacy-preserving analytics ingestion and deterministic aggregation."""
from __future__ import annotations
from dataclasses import replace
from datetime import datetime,timedelta
import hashlib
from typing import Callable
from packages.contracts import *
from services.data_service_common import *
from .repositories import UnitOfWorkFactory

class PlatformAnalyticsService:
    def __init__(self,uow:UnitOfWorkFactory,authorizer:DataServiceAuthorizer,audit:AuditSink,*,minimum_export_count:int=5,max_lateness_seconds:int=86400,future_clock_skew_seconds:int=60,clock:Callable[[],str]=utc_now)->None:
        self._uow=uow; self._auth=authorizer; self._audit=audit; self._minimum=minimum_export_count; self._lateness=max_lateness_seconds; self._future_skew=future_clock_skew_seconds; self._clock=clock
    def ingest(self,identity,tenant_id,event:AnalyticsEvent,idempotency_key,request_id):
        self._auth.require(identity,tenant_id,Permission.ANALYTICS_INGEST)
        require_idempotency_key(idempotency_key)
        if event.tenant_id!=tenant_id: raise AuthorizationDenied("tenant_isolation_violation","event tenant does not match authorized tenant")
        now_text=self._clock();now=datetime.fromisoformat(now_text.replace("Z","+00:00"));occurred=datetime.fromisoformat(event.occurred_at.replace("Z","+00:00"))
        if occurred<now-timedelta(seconds=self._lateness): raise PolicyViolation("late_event_rejected","event exceeds the configured lateness window")
        if occurred>now+timedelta(seconds=self._future_skew): raise PolicyViolation("future_event_rejected","event exceeds the configured future clock-skew allowance")
        canonical=replace(event,recorded_at=now_text)
        # Caller-supplied recorded_at is deliberately ignored and cannot affect replay.
        fp=contract_fingerprint({"event_id":event.event_id,"tenant_id":event.tenant_id,"product":event.product,"region":event.region,"event_type":event.event_type,"dimensions":event.dimensions,"occurred_at":event.occurred_at,"schema_version":event.schema_version}); scope=("analytics.ingest",tenant_id)
        with self._uow() as tx:
            old=tx.idempotency.get(scope,idempotency_key)
            if old is not None:
                if old[0]!=fp: raise Conflict("idempotency_conflict","key was reused with different event")
                tx.commit(); return old[1]
            result=tx.events.append(canonical); tx.idempotency.put(scope,idempotency_key,fp,result)
            tx.outbox.enqueue(OutboxRecord(deterministic_id("outbox",canonical.event_id),platform_event("analytics.event.accepted",tenant_id,request_id,{"resource_id":canonical.event_id,"region":canonical.region,"product":canonical.product.value},canonical.recorded_at),OutboxStatus.PENDING,0,None,1)); tx.commit()
        return result
    def get(self,identity,tenant_id,event_id):
        self._auth.require(identity,tenant_id,Permission.ANALYTICS_READ,allow_suspended=True)
        with self._uow() as tx:v=tx.events.get(event_id);tx.commit()
        if v is None or v.tenant_id!=tenant_id: raise ResourceNotFound("unknown_analytics_event","analytics event does not exist")
        return v
    def aggregate(self,identity,tenant_id,window:AggregationWindow):
        self._auth.require(identity,tenant_id,Permission.ANALYTICS_READ,allow_suspended=True)
        with self._uow() as tx: events=tx.events.range(tenant_id,window.start_at,window.end_at);tx.commit()
        groups={}
        for e in events:
            key=(e.product,e.region,e.event_type); groups[key]=groups.get(key,0)+1
        return tuple(MetricPoint(deterministic_id("metric",tenant_id,p.value,r,et.value,window.start_at,window.end_at),tenant_id,p,r,et,window,count) for (p,r,et),count in sorted(groups.items(),key=lambda x:(x[0][0].value,x[0][1],x[0][2].value)))
    def create_snapshot(self,identity,tenant_id,window,request_id):
        points=self.aggregate(identity,tenant_id,window); now=self._clock(); payload=contract_json(points); digest=hashlib.sha256(payload.encode()).hexdigest(); sid=deterministic_id("snapshot",tenant_id,window.start_at,window.end_at,digest); value=AnalyticsSnapshot(sid,tenant_id,window,points,now,digest)
        with self._uow() as tx:result=tx.snapshots.put(value);tx.commit();return result
    def export_snapshot(self,identity,tenant_id,snapshot_id):
        self._auth.require(identity,tenant_id,Permission.ANALYTICS_READ,allow_suspended=True)
        with self._uow() as tx:value=tx.snapshots.get(snapshot_id);tx.commit()
        if value is None or value.tenant_id!=tenant_id: raise ResourceNotFound("unknown_snapshot","analytics snapshot does not exist")
        return tuple(point for point in value.points if point.count>=self._minimum)
    def cross_tenant_export(self,identity,snapshot_ids):
        self._auth.require_platform_admin(identity); points=[]
        with self._uow() as tx:
            for sid in snapshot_ids:
                value=tx.snapshots.get(sid)
                if value is None: raise ResourceNotFound("unknown_snapshot","analytics snapshot does not exist")
                points.extend(point for point in value.points if point.count>=self._minimum)
            tx.commit()
        return tuple(sorted(points,key=lambda point:(point.tenant_id,point.metric_id,point.window.start_at)))
