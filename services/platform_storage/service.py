"""Metadata-only storage application service."""

from __future__ import annotations
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Callable
from packages.contracts import (ContentIntegrityMetadata, DownloadAuthorization, LegalHold, MalwareScanResult, MalwareScanStatus, ObjectClassification, ObjectStatus, ObjectVersion, Permission, Product, RetentionPolicy, StoredObject, UploadSession, UploadStatus, VerifiedSubjectIdentity, contract_fingerprint, utc_now)
from services.data_service_common import (AuditSink, Conflict, DataServiceAuthorizer, InvalidRequest, OutboxRecord, OutboxStatus, PlatformEvent, PolicyViolation, ResourceNotFound, ServiceAuditEvent, deterministic_id, platform_event, require_idempotency_key)
from .repositories import BlobStore, ObjectPage, UnitOfWorkFactory

MAX_CONTENT_LENGTH=100*1024*1024
MIN_DOWNLOAD_TTL_SECONDS=30
MAX_DOWNLOAD_TTL_SECONDS=3600

def _utc(value):
    parsed=datetime.fromisoformat(value.replace("Z","+00:00"))
    if parsed.utcoffset()!=timezone.utc.utcoffset(parsed): raise ValueError("timestamp must be UTC")
    return parsed

@dataclass(frozen=True)
class FinalizeResult:
    object: StoredObject
    object_version: ObjectVersion
    created: bool

class PlatformStorageService:
    def __init__(self,uow:UnitOfWorkFactory,blob_store:BlobStore,authorizer:DataServiceAuthorizer,audit:AuditSink,*,allowed_regions:frozenset[str],clock:Callable[[],str]=utc_now) -> None:
        self._uow=uow; self._blob=blob_store; self._auth=authorizer; self._audit=audit; self._regions=allowed_regions; self._clock=clock
    def _authorize(self,identity,tenant_id,permission,allow_suspended=False): self._auth.require(identity,tenant_id,permission,allow_suspended=allow_suspended)
    @staticmethod
    def _scope(op,tenant,identity): return (op,tenant,identity.subject)
    @staticmethod
    def _replay(record,fingerprint):
        if record[0]!=fingerprint: raise Conflict("idempotency_conflict","idempotency key was reused with different input")
        return record[1]
    def initiate_upload(self,identity:VerifiedSubjectIdentity,tenant_id:str,product:Product,region:str,classification:ObjectClassification,content_length:int,integrity:ContentIntegrityMetadata,idempotency_key:str,request_id:str,*,object_id:str|None=None,retain_until:str|None=None)->UploadSession:
        self._authorize(identity,tenant_id,Permission.STORAGE_WRITE)
        require_idempotency_key(idempotency_key)
        if region not in self._regions: raise PolicyViolation("region_policy_denied","storage region is not allowed")
        if not isinstance(content_length,int) or isinstance(content_length,bool) or not 0<=content_length<=MAX_CONTENT_LENGTH: raise InvalidRequest("invalid_content_length","content length is outside the configured limit")
        if classification in {ObjectClassification.RESTRICTED,ObjectClassification.REGULATED} and retain_until is None: raise PolicyViolation("retention_required","restricted and regulated objects require retention metadata")
        oid=object_id or deterministic_id("obj",tenant_id,request_id); upload_id=deterministic_id("upl",tenant_id,request_id); key=deterministic_id("blob",tenant_id,upload_id)
        timestamp=self._clock()
        if classification in {ObjectClassification.RESTRICTED,ObjectClassification.REGULATED} and _utc(retain_until)<=_utc(timestamp): raise PolicyViolation("retention_must_be_future","restricted and regulated retention must be future-dated")
        session=UploadSession(upload_id,tenant_id,product,region,oid,classification,key,content_length,integrity,UploadStatus.INITIATED,timestamp,(_utc(timestamp)+timedelta(hours=1)).isoformat(),1)
        fp=contract_fingerprint({"tenant_id":tenant_id,"product":product.value,"region":region,"classification":classification.value,"content_length":content_length,"integrity":integrity,"object_id":object_id,"retain_until":retain_until})
        scope=self._scope("storage.upload.initiate",tenant_id,identity)
        with self._uow() as tx:
            old=tx.idempotency.get(scope,idempotency_key)
            if old is not None: result=self._replay(old,fp); tx.commit(); return result
            tx.uploads.create(session)
            if retain_until is not None:
                tx.policies.put_retention(RetentionPolicy(deterministic_id("ret",oid),tenant_id,oid,retain_until,timestamp,1),0)
            tx.idempotency.put(scope,idempotency_key,fp,session)
            tx.outbox.enqueue(OutboxRecord(deterministic_id("outbox",upload_id),platform_event("storage.upload.initiated",tenant_id,request_id,{"resource_id":oid,"region":region,"product":product.value},timestamp),OutboxStatus.PENDING,0,None,1))
            tx.commit()
        self._audit.emit(ServiceAuditEvent("storage.upload_initiated",request_id,"succeeded",tenant_id,identity.subject,resource_id=oid))
        return session
    def finalize_upload(self,identity,tenant_id,upload_id,idempotency_key,request_id)->FinalizeResult:
        self._authorize(identity,tenant_id,Permission.STORAGE_WRITE); require_idempotency_key(idempotency_key); scope=self._scope("storage.upload.finalize",tenant_id,identity); fp=contract_fingerprint({"upload_id":upload_id})
        with self._uow() as tx:
            old=tx.idempotency.get(scope,idempotency_key)
            if old is not None: result=self._replay(old,fp); tx.commit(); return result
            session=tx.uploads.get(upload_id)
            if session is None or session.tenant_id!=tenant_id: raise ResourceNotFound("unknown_upload","upload does not exist")
            if session.status is not UploadStatus.INITIATED: raise Conflict("invalid_upload_state","upload is not active")
            if _utc(session.expires_at)<=_utc(self._clock()): raise PolicyViolation("upload_expired","expired uploads cannot be finalized")
            stat=self._blob.stat(session.storage_key)
            if stat is None: raise PolicyViolation("blob_not_found","blob provider has no staged object")
            if stat.content_length!=session.expected_content_length or stat.algorithm!=session.expected_integrity.algorithm or stat.digest!=session.expected_integrity.digest: raise PolicyViolation("checksum_mismatch","staged blob does not match declared integrity metadata")
            timestamp=self._clock(); current=tx.objects.get(session.object_id); number=1 if current is None else current.current_version+1
            version=ObjectVersion(deterministic_id("ov",session.object_id,number),session.object_id,tenant_id,session.product,session.region,number,session.storage_key,stat.content_length,session.expected_integrity,MalwareScanStatus.PENDING,timestamp,identity.subject)
            if current is None:
                obj=StoredObject(session.object_id,tenant_id,session.product,session.region,session.classification,ObjectStatus.ACTIVE,number,timestamp,timestamp,1); tx.objects.create(obj)
            else:
                if current.tenant_id!=tenant_id: raise ResourceNotFound("unknown_object","object does not exist")
                if current.status is ObjectStatus.DELETED: raise PolicyViolation("object_deleted","deleted objects must be restored before a new version is created")
                obj=replace(current,current_version=number,status=ObjectStatus.ACTIVE,updated_at=timestamp,version=current.version+1); tx.objects.update(obj,current.version)
            tx.versions.append(version); tx.uploads.update(replace(session,status=UploadStatus.FINALIZED,version=session.version+1),session.version)
            result=FinalizeResult(obj,version,True); tx.idempotency.put(scope,idempotency_key,fp,result)
            tx.outbox.enqueue(OutboxRecord(deterministic_id("outbox",version.object_version_id),platform_event("storage.object.finalized",tenant_id,request_id,{"resource_id":obj.object_id,"region":obj.region,"product":obj.product.value},timestamp),OutboxStatus.PENDING,0,None,1)); tx.commit()
        self._audit.emit(ServiceAuditEvent("storage.upload_finalized",request_id,"succeeded",tenant_id,identity.subject,resource_id=session.object_id)); return result
    def abort_upload(self,identity,tenant_id,upload_id,expected_version,request_id):
        self._authorize(identity,tenant_id,Permission.STORAGE_WRITE)
        with self._uow() as tx:
            s=tx.uploads.get(upload_id)
            if s is None or s.tenant_id!=tenant_id: raise ResourceNotFound("unknown_upload","upload does not exist")
            if s.status is not UploadStatus.INITIATED: raise Conflict("invalid_upload_state","upload cannot be aborted")
            result=tx.uploads.update(replace(s,status=UploadStatus.ABORTED,version=s.version+1),expected_version); tx.commit(); return result
    def get_object(self,identity,tenant_id,object_id):
        self._authorize(identity,tenant_id,Permission.STORAGE_READ,True)
        with self._uow() as tx:
            obj=tx.objects.get(object_id); tx.commit()
        if obj is None or obj.tenant_id!=tenant_id: raise ResourceNotFound("unknown_object","object does not exist")
        return obj
    def get_version(self,identity,tenant_id,object_version_id):
        self._authorize(identity,tenant_id,Permission.STORAGE_READ,True)
        with self._uow() as tx: value=tx.versions.get(object_version_id); tx.commit()
        if value is None or value.tenant_id!=tenant_id: raise ResourceNotFound("unknown_object_version","object version does not exist")
        return value
    def list_objects(self,identity,tenant_id,limit=50,cursor=None)->ObjectPage:
        self._authorize(identity,tenant_id,Permission.STORAGE_READ,True)
        if not 1<=limit<=100: raise InvalidRequest("invalid_pagination","limit must be 1 to 100")
        with self._uow() as tx: page=tx.objects.list(tenant_id,limit,cursor); tx.commit(); return page
    def mark_deleted(self,identity,tenant_id,object_id,expected_version,request_id):
        self._authorize(identity,tenant_id,Permission.STORAGE_MANAGE)
        with self._uow() as tx:
            obj=tx.objects.get(object_id)
            if obj is None or obj.tenant_id!=tenant_id: raise ResourceNotFound("unknown_object","object does not exist")
            hold=tx.policies.get_hold(object_id); retention=tx.policies.get_retention(object_id); now=self._clock()
            if hold is not None and hold.active: raise PolicyViolation("legal_hold_active","object is under legal hold")
            if retention is not None and _utc(retention.retain_until)>_utc(now): raise PolicyViolation("retention_active","object retention period has not elapsed")
            result=tx.objects.update(replace(obj,status=ObjectStatus.DELETED,updated_at=now,version=obj.version+1),expected_version); tx.commit(); return result
    def restore(self,identity,tenant_id,object_id,expected_version,request_id):
        self._authorize(identity,tenant_id,Permission.STORAGE_MANAGE)
        with self._uow() as tx:
            obj=tx.objects.get(object_id)
            if obj is None or obj.tenant_id!=tenant_id: raise ResourceNotFound("unknown_object","object does not exist")
            result=tx.objects.update(replace(obj,status=ObjectStatus.ACTIVE,updated_at=self._clock(),version=obj.version+1),expected_version); tx.commit(); return result
    def apply_retention(self,identity,tenant_id,object_id,retain_until,expected_version,request_id,*,override=False,reason_code=None):
        self._authorize(identity,tenant_id,Permission.STORAGE_MANAGE); obj=self.get_object(identity,tenant_id,object_id); now=self._clock()
        if obj.classification in {ObjectClassification.RESTRICTED,ObjectClassification.REGULATED} and _utc(retain_until)<=_utc(now): raise PolicyViolation("retention_must_be_future","restricted and regulated retention must be future-dated")
        with self._uow() as tx:
            old=tx.policies.get_retention(object_id)
            if old is not None and _utc(retain_until)<_utc(old.retain_until):
                if not override: raise PolicyViolation("retention_shortening_denied","active retention cannot be shortened")
                self._auth.require_platform_admin(identity)
                if not reason_code: raise InvalidRequest("override_reason_required","retention override requires a reason code")
            value=RetentionPolicy(deterministic_id("ret",object_id),tenant_id,object_id,retain_until,old.created_at if old else now,(old.version+1 if old else 1))
            result=tx.policies.put_retention(value,expected_version); tx.commit()
        if old is not None and _utc(retain_until)<_utc(old.retain_until): self._audit.emit(ServiceAuditEvent("storage.retention_overridden",request_id,"succeeded",tenant_id,identity.subject,reason_code=reason_code,resource_id=object_id))
        return result
    def set_legal_hold(self,identity,tenant_id,object_id,active,reason_code,expected_version,request_id):
        self._authorize(identity,tenant_id,Permission.STORAGE_MANAGE); self.get_object(identity,tenant_id,object_id)
        with self._uow() as tx:
            old=tx.policies.get_hold(object_id); now=self._clock(); value=LegalHold(deterministic_id("hold",object_id),tenant_id,object_id,active,reason_code,old.created_at if old else now,now,(old.version+1 if old else 1))
            if old and old.version!=expected_version: raise Conflict("stale_version","legal hold version is stale")
            if old is None and expected_version!=0: raise Conflict("stale_version","legal hold version is stale")
            result=tx.policies.put_hold(value); tx.commit(); return result
    def record_malware_scan(self,identity,tenant_id,object_version_id,status,request_id):
        self._auth.require_executor(identity,tenant_id,allow_suspended=True)
        with self._uow() as tx:
            value=tx.versions.get(object_version_id)
            if value is None or value.tenant_id!=tenant_id: raise ResourceNotFound("unknown_object_version","object version does not exist")
            result=MalwareScanResult(deterministic_id("scan",object_version_id,request_id),object_version_id,value.object_id,tenant_id,value.product,value.region,status,self._clock(),deterministic_id("scanner",identity.subject))
            tx.scans.append(result)
            if status is MalwareScanStatus.INFECTED: tx.outbox.enqueue(OutboxRecord(deterministic_id("outbox","malware",object_version_id),platform_event("storage.object.malware-detected",tenant_id,request_id,{"resource_id":value.object_id,"region":value.region,"product":value.product.value}),OutboxStatus.PENDING,0,None,1))
            tx.commit(); return result
    def current_scan_status(self,identity,tenant_id,object_version_id):
        value=self.get_version(identity,tenant_id,object_version_id)
        with self._uow() as tx: history=tx.scans.list(object_version_id);tx.commit()
        return history[-1].status if history else MalwareScanStatus.PENDING
    def verify_checksum(self,identity,tenant_id,object_version_id):
        value=self.get_version(identity,tenant_id,object_version_id); stat=self._blob.stat(value.storage_key)
        return stat is not None and stat.content_length==value.content_length and stat.algorithm==value.integrity.algorithm and stat.digest==value.integrity.digest
    def authorize_download(self,identity,tenant_id,object_id,request_id,ttl_seconds=300):
        obj=self.get_object(identity,tenant_id,object_id)
        if not isinstance(ttl_seconds,int) or isinstance(ttl_seconds,bool) or not MIN_DOWNLOAD_TTL_SECONDS<=ttl_seconds<=MAX_DOWNLOAD_TTL_SECONDS: raise InvalidRequest("invalid_download_ttl","download TTL is outside the permitted range")
        if obj.region not in self._regions: raise PolicyViolation("region_policy_unavailable","object region policy is unavailable")
        if obj.status is ObjectStatus.DELETED: raise PolicyViolation("object_deleted","deleted objects cannot be downloaded")
        with self._uow() as tx: version=tx.versions.get_number(object_id,obj.current_version);retention=tx.policies.get_retention(object_id);hold=tx.policies.get_hold(object_id);tx.commit()
        if version is None: raise ResourceNotFound("unknown_object_version","object version does not exist")
        if obj.classification in {ObjectClassification.RESTRICTED,ObjectClassification.REGULATED} and (retention is None or hold is None): raise PolicyViolation("governance_policy_unavailable","retention and legal-hold policy must be available")
        if self.current_scan_status(identity,tenant_id,version.object_version_id) is not MalwareScanStatus.CLEAN: raise PolicyViolation("object_not_releasable","object must pass malware scanning")
        if not self.verify_checksum(identity,tenant_id,version.object_version_id): raise PolicyViolation("checksum_mismatch","object checksum verification failed")
        expires=(_utc(self._clock())+timedelta(seconds=ttl_seconds)).isoformat(); aid=deterministic_id("download",tenant_id,object_id,request_id,expires)
        return DownloadAuthorization(aid,tenant_id,object_id,version.object_version_id,deterministic_id("opaque",aid),expires)
