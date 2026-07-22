"""Authenticated /v1 HTTP boundary for platform analytics."""
from http import HTTPStatus
from packages.contracts import *
from services.data_service_common import InvalidRequest
from services.data_service_http import handler
def router(s):
    def route(m,p,q,b,i,rid,key):
        if m=="POST" and p==["v1","events"]:
            if b is None:raise InvalidRequest("invalid_request","request body is required")
            d=b.get("dimensions",{});event=AnalyticsEvent(b["event_id"],b["tenant_id"],Product(b["product"]),b["region"],AnalyticsEventType(b["event_type"]),AnalyticsDimensions(**d),b["occurred_at"],b["recorded_at"]);return s.ingest(i,b["tenant_id"],event,key or "",rid),HTTPStatus.CREATED
        if m=="GET" and len(p)==3 and p[1]=="events":return s.get(i,q.get("tenant_id",[""])[0],p[2]),HTTPStatus.OK
        if m=="POST" and p==["v1","snapshots"]:
            return s.create_snapshot(i,b["tenant_id"],AggregationWindow(b["start_at"],b["end_at"]),rid),HTTPStatus.CREATED
        if m=="POST" and p==["v1","metrics"]:return s.aggregate(i,b["tenant_id"],AggregationWindow(b["start_at"],b["end_at"])),HTTPStatus.OK
        if m=="GET" and len(p)==4 and p[1]=="snapshots" and p[3]=="export":return s.export_snapshot(i,q.get("tenant_id",[""])[0],p[2]),HTTPStatus.OK
        raise InvalidRequest("unknown_route","route not found")
    return route
def make_handler(service,authenticator,audit):return handler("platform-analytics",authenticator,audit,router(service))
def main():raise RuntimeError("platform-analytics requires injected production repositories")
