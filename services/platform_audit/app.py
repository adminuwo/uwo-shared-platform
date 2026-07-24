"""Authenticated /v1 HTTP boundary for durable platform audit."""
from http import HTTPStatus
from services.data_service_common import InvalidRequest
from services.data_service_http import handler
def router(s):
    def route(m,p,q,b,i,rid,key):
        if m=="POST" and p==["v1","events"]:return s.append(i,b["tenant_id"],b["action"],b["outcome"],rid,b.get("attributes",{}),b.get("actor_subject")),HTTPStatus.CREATED
        if m=="GET" and p==["v1","events"]:return s.list(i,q.get("tenant_id",[""])[0],int(q.get("limit",["50"])[0]),q.get("cursor",[None])[0]),HTTPStatus.OK
        if m=="POST" and p==["v1","verify"]:return s.verify(i,b["tenant_id"]),HTTPStatus.OK
        if m=="POST" and p==["v1","checkpoints"]:return s.checkpoint(i,b["tenant_id"],rid),HTTPStatus.CREATED
        if m=="POST" and p==["v1","exports"]:return s.export(i,b["tenant_id"],rid,b.get("first_sequence"),b.get("last_sequence")),HTTPStatus.CREATED
        if m=="POST" and len(p)==4 and p[1]=="checkpoints" and p[3]=="verify":return {"valid":s.verify_checkpoint(i,b["tenant_id"],p[2])},HTTPStatus.OK
        if m=="PUT" and p==["v1","retention"]:return s.set_retention(i,b["tenant_id"],b["retain_until"],b["legal_hold"],b.get("expected_version"),rid),HTTPStatus.OK
        raise InvalidRequest("unknown_route","route not found")
    return route
def make_handler(service,authenticator,audit):return handler("platform-audit",authenticator,audit,router(service))
def main():raise RuntimeError("platform-audit requires injected durable production repositories")
