"""Authenticated /v1 HTTP boundary for platform storage."""
from http import HTTPStatus
from packages.contracts import *
from services.data_service_common import InvalidRequest
from services.data_service_http import handler,serve
def _need(body,*names):
    if body is None or any(name not in body for name in names):raise InvalidRequest("invalid_request","request fields do not match the contract")
def router(service):
    def route(method,p,q,b,i,rid,key):
        if method=="POST" and p==["v1","uploads"]:
            _need(b,"tenant_id","product","region","classification","content_length","algorithm","digest");return service.initiate_upload(i,b["tenant_id"],Product(b["product"]),b["region"],ObjectClassification(b["classification"]),b["content_length"],ContentIntegrityMetadata(b["algorithm"],b["digest"]),key or "",rid,object_id=b.get("object_id"),retain_until=b.get("retain_until")),HTTPStatus.CREATED
        if method=="POST" and len(p)==4 and p[1]=="uploads" and p[3]=="finalize":_need(b,"tenant_id");return service.finalize_upload(i,b["tenant_id"],p[2],key or "",rid),HTTPStatus.OK
        if method=="POST" and len(p)==4 and p[1]=="uploads" and p[3]=="abort":_need(b,"tenant_id","expected_version");return service.abort_upload(i,b["tenant_id"],p[2],b["expected_version"],rid),HTTPStatus.OK
        if method=="GET" and p==["v1","objects"]:return service.list_objects(i,q.get("tenant_id",[""])[0],int(q.get("limit",["50"])[0]),q.get("cursor",[None])[0]),HTTPStatus.OK
        if method=="GET" and len(p)==3 and p[1]=="objects":return service.get_object(i,q.get("tenant_id",[""])[0],p[2]),HTTPStatus.OK
        if method=="GET" and len(p)==3 and p[1]=="object-versions":return service.get_version(i,q.get("tenant_id",[""])[0],p[2]),HTTPStatus.OK
        if method=="DELETE" and len(p)==3 and p[1]=="objects":return service.mark_deleted(i,q.get("tenant_id",[""])[0],p[2],int(q.get("version",["0"])[0]),rid),HTTPStatus.OK
        if method=="POST" and len(p)==4 and p[1]=="objects" and p[3]=="restore":_need(b,"tenant_id","expected_version");return service.restore(i,b["tenant_id"],p[2],b["expected_version"],rid),HTTPStatus.OK
        if method=="PUT" and len(p)==4 and p[1]=="objects" and p[3]=="retention":_need(b,"tenant_id","retain_until","expected_version");return service.apply_retention(i,b["tenant_id"],p[2],b["retain_until"],b["expected_version"],rid,override=b.get("override",False),reason_code=b.get("reason_code")),HTTPStatus.OK
        if method=="PUT" and len(p)==4 and p[1]=="objects" and p[3]=="legal-hold":_need(b,"tenant_id","active","reason_code","expected_version");return service.set_legal_hold(i,b["tenant_id"],p[2],b["active"],b["reason_code"],b["expected_version"],rid),HTTPStatus.OK
        if method=="POST" and len(p)==4 and p[1]=="object-versions" and p[3]=="malware-scan":_need(b,"tenant_id","status");return service.record_malware_scan(i,b["tenant_id"],p[2],MalwareScanStatus(b["status"]),key or "",rid),HTTPStatus.OK
        if method=="POST" and len(p)==4 and p[1]=="objects" and p[3]=="download-authorizations":_need(b,"tenant_id");return service.authorize_download(i,b["tenant_id"],p[2],rid,b.get("ttl_seconds",300)),HTTPStatus.CREATED
        raise InvalidRequest("unknown_route","route not found")
    return route
def make_handler(service,authenticator,audit):return handler("platform-storage",authenticator,audit,router(service))
def main():raise RuntimeError("platform-storage requires injected production repositories and blob provider")
