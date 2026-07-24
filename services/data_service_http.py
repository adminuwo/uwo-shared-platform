"""Hardened authenticated HTTP foundation for Phase 3C internal APIs."""
from __future__ import annotations
import json,re,uuid
from dataclasses import fields,is_dataclass
from enum import Enum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from typing import Any,Callable,Mapping,Protocol
from urllib.parse import parse_qs,unquote,urlsplit
from packages.contracts import VerifiedSubjectIdentity
from services.platform_control_plane.auth import AuthenticationError,Authenticator
from .data_service_common import *
REQUEST_ID=re.compile(r"^[A-Za-z0-9._:-]{1,128}$");MAX_REQUEST_BYTES=65_536
class Router(Protocol):
    def __call__(self,method:str,parts:list[str],query:dict[str,list[str]],body:dict[str,Any]|None,identity:VerifiedSubjectIdentity,request_id:str,idempotency_key:str|None)->tuple[Any,HTTPStatus]:...
def json_value(value):
    if is_dataclass(value):return {f.name:json_value(getattr(value,f.name)) for f in fields(value)}
    if isinstance(value,Enum):return value.value
    if isinstance(value,Mapping):return {k:json_value(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)):return [json_value(x) for x in value]
    return value
def handler(service_name:str,authenticator:Authenticator,audit:AuditSink,router:Router)->type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version=f"UWO-{service_name}/0.3"
        def _rid(self):
            value=self.headers.get("X-Request-ID","");return value if REQUEST_ID.fullmatch(value) else str(uuid.uuid4())
        def _respond(self,status,body,rid):
            payload=json.dumps(json_value(body),separators=(",",":"),allow_nan=False).encode();self.send_response(status)
            for k,v in (("Content-Type","application/json"),("Content-Length",str(len(payload))),("Cache-Control","no-store"),("X-Content-Type-Options","nosniff"),("X-Frame-Options","DENY"),("Referrer-Policy","no-referrer"),("X-Request-ID",rid)):self.send_header(k,v)
            if status is HTTPStatus.UNAUTHORIZED:self.send_header("WWW-Authenticate",f'Bearer realm="{service_name}"')
            self.end_headers();self.wfile.write(payload)
        def _error(self,status,code,message,rid):self._respond(status,{"error":{"code":code,"message":message},"request_id":rid},rid)
        def _audit_denial(self,rid,identity,code):audit.emit(ServiceAuditEvent(f"{service_name}.administration_denied",rid,"denied",actor_subject=identity.subject if identity else None,reason_code=code))
        def _body(self):
            if self.command in {"GET","DELETE"}:return None
            try:length=int(self.headers.get("Content-Length","0"))
            except ValueError:raise InvalidRequest("invalid_request","Content-Length must be an integer")
            if length>MAX_REQUEST_BYTES:raise PolicyViolation("request_too_large","request body exceeds 65536 bytes")
            if length<=0:raise InvalidRequest("invalid_request","request body is required")
            try:value=json.loads(self.rfile.read(length))
            except (json.JSONDecodeError,UnicodeDecodeError) as exc:raise InvalidRequest("invalid_request","request body must be valid JSON") from exc
            if not isinstance(value,dict):raise InvalidRequest("invalid_request","request body must be a JSON object")
            return value
        def _route(self):
            rid=self._rid();parsed=urlsplit(self.path)
            if self.command=="GET" and parsed.path in {"/healthz","/v1/health"}:self._respond(HTTPStatus.OK,{"status":"ok","service":service_name,"request_id":rid},rid);return
            if not parsed.path.startswith("/v1/"):self._error(HTTPStatus.NOT_FOUND,"not_found","route not found",rid);return
            identity=None
            try:
                identity=authenticator.authenticate(self.headers.get("Authorization",""));parts=[unquote(x) for x in parsed.path.strip("/").split("/")];result,status=router(self.command,parts,parse_qs(parsed.query,keep_blank_values=True),self._body(),identity,rid,self.headers.get("Idempotency-Key"));self._respond(status,{"data":result,"request_id":rid},rid)
            except AuthenticationError as exc:self._audit_denial(rid,identity,exc.code);self._error(HTTPStatus.UNAUTHORIZED,exc.code,str(exc),rid)
            except AuthorizationDenied as exc:self._audit_denial(rid,identity,exc.code);self._error(HTTPStatus.FORBIDDEN,exc.code,str(exc),rid)
            except ResourceNotFound as exc:self._audit_denial(rid,identity,exc.code);self._error(HTTPStatus.NOT_FOUND,exc.code,str(exc),rid)
            except Conflict as exc:self._audit_denial(rid,identity,exc.code);self._error(HTTPStatus.CONFLICT,exc.code,str(exc),rid)
            except InfrastructureUnavailable as exc:audit.emit(ServiceAuditEvent(f"{service_name}.infrastructure_failure",rid,"failed",actor_subject=identity.subject if identity else None,reason_code=exc.code));self._error(HTTPStatus.SERVICE_UNAVAILABLE,exc.code,"a required service integration is unavailable",rid)
            except PolicyViolation as exc:self._audit_denial(rid,identity,exc.code);self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE if exc.code=="request_too_large" else HTTPStatus.UNPROCESSABLE_ENTITY,exc.code,str(exc),rid)
            except InvalidRequest as exc:self._audit_denial(rid,identity,exc.code);self._error(HTTPStatus.NOT_FOUND if exc.code=="unknown_route" else HTTPStatus.BAD_REQUEST,exc.code,str(exc),rid)
            except (TypeError,ValueError):self._audit_denial(rid,identity,"invalid_request");self._error(HTTPStatus.BAD_REQUEST,"invalid_request","request contains invalid values",rid)
            except Exception:audit.emit(ServiceAuditEvent(f"{service_name}.internal_error",rid,"failed",actor_subject=identity.subject if identity else None,reason_code="internal_error"));self._error(HTTPStatus.INTERNAL_SERVER_ERROR,"internal_error","an internal error occurred",rid)
        do_GET=_route;do_POST=_route;do_PUT=_route;do_PATCH=_route;do_DELETE=_route
        def log_message(self,format,*args):return
    return Handler
def serve(bind:str,port:int,handler_type:type[BaseHTTPRequestHandler]):ThreadingHTTPServer((bind,port),handler_type).serve_forever()
