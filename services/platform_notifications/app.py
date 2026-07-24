"""Authenticated /v1 HTTP boundary for platform notifications."""
from http import HTTPStatus
from packages.contracts import *
from services.data_service_common import InvalidRequest
from services.data_service_http import handler
def _need(b,*n):
    if b is None or any(x not in b for x in n):raise InvalidRequest("invalid_request","request fields do not match the contract")
def router(s):
    def route(m,p,q,b,i,rid,key):
        if m=="POST" and p==["v1","templates"]:_need(b,"tenant_id","template_id","product","region","channel","content_reference");return s.register_template(i,b["tenant_id"],b["template_id"],Product(b["product"]),b["region"],NotificationChannel(b["channel"]),b["content_reference"],tuple(b.get("variable_keys",[])),rid),HTTPStatus.CREATED
        if m=="POST" and len(p)==4 and p[1]=="templates" and p[3]=="versions":_need(b,"tenant_id","channel","content_reference","expected_version");return s.add_template_version(i,b["tenant_id"],p[2],NotificationChannel(b["channel"]),b["content_reference"],tuple(b.get("variable_keys",[])),b["expected_version"],rid),HTTPStatus.CREATED
        if m=="POST" and len(p)==4 and p[1]=="templates" and p[3]=="activate":_need(b,"tenant_id","version_number","expected_version");return s.activate_template(i,b["tenant_id"],p[2],b["version_number"],b["expected_version"],rid),HTTPStatus.OK
        if m=="POST" and p==["v1","notifications"]:_need(b,"tenant_id","product","region","template_id","channel","recipient_reference","deduplication_key");return s.create_notification(i,b["tenant_id"],Product(b["product"]),b["region"],b["template_id"],NotificationChannel(b["channel"]),b["recipient_reference"],b["deduplication_key"],rid),HTTPStatus.CREATED
        if m=="GET" and p==["v1","notifications"]:return s.list(i,q.get("tenant_id",[""])[0],int(q.get("limit",["50"])[0]),q.get("cursor",[None])[0]),HTTPStatus.OK
        if m=="GET" and len(p)==3 and p[1]=="notifications":return s.get(i,q.get("tenant_id",[""])[0],p[2]),HTTPStatus.OK
        if m=="POST" and len(p)==4 and p[3]=="dispatch":_need(b,"tenant_id");return s.dispatch(i,b["tenant_id"],p[2],rid),HTTPStatus.OK
        if m=="POST" and len(p)==4 and p[3]=="cancel":_need(b,"tenant_id","expected_version");return s.cancel(i,b["tenant_id"],p[2],b["expected_version"],rid),HTTPStatus.OK
        if m=="PUT" and p==["v1","preferences"]:_need(b,"tenant_id","subject_reference","channel","enabled");return s.set_preference(i,b["tenant_id"],b["subject_reference"],NotificationChannel(b["channel"]),b["enabled"],b.get("expected_version"),rid),HTTPStatus.OK
        if m=="GET" and p==["v1","preferences"]:return s.get_preference(i,q.get("tenant_id",[""])[0],q.get("subject_reference",[""])[0],NotificationChannel(q.get("channel",[""])[0])),HTTPStatus.OK
        if m=="GET" and len(p)==3 and p[1]=="dead-letters":return s.get_dead_letter(i,q.get("tenant_id",[""])[0],p[2]),HTTPStatus.OK
        raise InvalidRequest("unknown_route","route not found")
    return route
def make_handler(service,authenticator,audit):return handler("platform-notifications",authenticator,audit,router(service))
def main():raise RuntimeError("platform-notifications requires injected production repositories and providers")
