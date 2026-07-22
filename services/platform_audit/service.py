"""Append-only hash-chain audit application service."""
from __future__ import annotations
from dataclasses import replace
import hashlib
from typing import Callable,Mapping,Any
from packages.contracts import *
from services.data_service_common import *
from .repositories import UnitOfWorkFactory

def _event_hash(tenant_id,sequence,action,outcome,occurred_at,request_id,actor_subject,attributes,previous_hash):
    payload={"tenant_id":tenant_id,"sequence":sequence,"action":action,"outcome":outcome,"occurred_at":occurred_at,"request_id":request_id,"actor_subject":actor_subject,"attributes":attributes,"previous_hash":previous_hash,"schema_version":"1"}
    return hashlib.sha256(contract_json(payload).encode()).hexdigest()

class PlatformAuditService:
    def __init__(self,uow:UnitOfWorkFactory,authorizer:DataServiceAuthorizer,audit:AuditSink,clock:Callable[[],str]=utc_now)->None:self._uow=uow;self._auth=authorizer;self._audit=audit;self._clock=clock
    def append(self,identity,tenant_id,action,outcome,request_id,attributes:Mapping[str,Any],actor_subject=None):
        self._auth.require_executor(identity,tenant_id,allow_suspended=True);now=self._clock()
        with self._uow() as tx:
            sequence,previous=tx.stream.next_sequence(tenant_id);current=_event_hash(tenant_id,sequence,action,outcome,now,request_id,actor_subject,attributes,previous);eid=deterministic_id("audit",tenant_id,sequence,current);value=DurableAuditEvent(eid,tenant_id,sequence,action,outcome,now,request_id,actor_subject,attributes,previous,current,True);result=tx.stream.append(value);tx.commit();return result
    def list(self,identity,tenant_id,limit=50,cursor=None):
        self._auth.require(identity,tenant_id,Permission.AUDIT_READ,allow_suspended=True)
        if not 1<=limit<=100:raise InvalidRequest("invalid_pagination","limit must be 1 to 100")
        with self._uow() as tx:page=tx.stream.list(tenant_id,limit,cursor);tx.commit();return page
    def verify(self,identity,tenant_id):
        self._auth.require(identity,tenant_id,Permission.AUDIT_VERIFY,allow_suspended=True);return self._verify_unchecked(tenant_id)
    def _verify_unchecked(self,tenant_id):
        with self._uow() as tx:events=tx.stream.range(tenant_id,None,None);tx.commit()
        previous="0"*64
        for event in events:
            expected=_event_hash(event.tenant_id,event.sequence,event.action,event.outcome,event.occurred_at,event.request_id,event.actor_subject,event.attributes,previous)
            if event.previous_hash!=previous or event.current_hash!=expected:return AuditIntegrityProof(tenant_id,False,len(events),event.sequence,self._clock())
            previous=event.current_hash
        return AuditIntegrityProof(tenant_id,True,len(events),None,self._clock())
    def checkpoint(self,identity,tenant_id,request_id):
        self._auth.require(identity,tenant_id,Permission.AUDIT_VERIFY,allow_suspended=True)
        proof=self._verify_unchecked(tenant_id)
        if not proof.valid:raise PolicyViolation("audit_integrity_failure","audit hash chain verification failed")
        with self._uow() as tx:
            events=tx.stream.range(tenant_id,None,None)
            if not events:raise ResourceNotFound("empty_audit_stream","audit stream is empty")
            last=events[-1];value=AuditCheckpoint(deterministic_id("checkpoint",tenant_id,last.sequence,last.current_hash),tenant_id,last.sequence,last.current_hash,self._clock());result=tx.checkpoints.create(value);tx.commit();return result
    def verify_checkpoint(self,identity,tenant_id,checkpoint_id):
        self._auth.require(identity,tenant_id,Permission.AUDIT_VERIFY,allow_suspended=True)
        with self._uow() as tx:
            checkpoint=tx.checkpoints.get(checkpoint_id);events=tx.stream.range(tenant_id,None,None);tx.commit()
        if checkpoint is None or checkpoint.tenant_id!=tenant_id:raise ResourceNotFound("unknown_checkpoint","checkpoint does not exist")
        event=next((x for x in events if x.sequence==checkpoint.through_sequence),None);return event is not None and event.current_hash==checkpoint.event_hash
    def export(self,identity,tenant_id,request_id,first_sequence=None,last_sequence=None):
        self._auth.require(identity,tenant_id,Permission.AUDIT_EXPORT,allow_suspended=True);proof=self._verify_unchecked(tenant_id)
        if not proof.valid:raise PolicyViolation("audit_integrity_failure","cannot export an invalid audit stream")
        with self._uow() as tx:
            events=tx.stream.range(tenant_id,first_sequence,last_sequence);joined="".join(x.current_hash for x in events);digest=hashlib.sha256(joined.encode()).hexdigest();first=events[0].sequence if events else 0;last=events[-1].sequence if events else 0;manifest=AuditExportManifest(deterministic_id("audit-export",tenant_id,first,last,digest),tenant_id,first,last,len(events),digest,self._clock());tx.exports.create(manifest);tx.commit();return manifest,events
    def set_retention(self,identity,tenant_id,retain_until,legal_hold,expected_version,request_id):
        self._auth.require_platform_admin(identity)
        with self._uow() as tx:
            old=tx.retention.get(tenant_id);value=AuditRetentionPolicy(deterministic_id("audit-retention",tenant_id),tenant_id,retain_until,legal_hold,old.created_at if old else self._clock(),old.version+1 if old else 1);result=tx.retention.put(value,expected_version);tx.commit();return result

class DurableAuditEventPublisher:
    """Provider-neutral bridge from platform events into the audit stream."""
    def __init__(self,service:PlatformAuditService,identity:VerifiedSubjectIdentity)->None:self._service=service;self._identity=identity
    def publish(self,event:PlatformEvent)->None:
        attrs={key:value for key,value in event.attributes.items() if key in {"resource_id","reason_code","provider_id","region","product","permission","pseudonymous_subject_id","status"}}
        self._service.append(self._identity,event.tenant_id,event.event_type,"recorded",event.request_id,attrs)
