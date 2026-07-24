"""Thread-safe rollback-capable durable audit test repositories."""
from __future__ import annotations
from threading import RLock
from services.data_service_common import Conflict,InMemoryOutbox,RepositoryIntegrityError
from .repositories import AuditPage
ZERO_HASH="0"*64
class InMemoryAuditState:
    def __init__(self): self.events={};self.checkpoints={};self.exports={};self.retentions={};self.source_events={};self.outbox=InMemoryOutbox();self.lock=RLock();self.fail_next=None
    def _fail(self,op):
        if self.fail_next==op:self.fail_next=None;raise RepositoryIntegrityError(f"injected {op} failure")
    def snapshot(self):return {tenant:list(events) for tenant,events in self.events.items()},dict(self.checkpoints),dict(self.exports),dict(self.retentions),dict(self.source_events),self.outbox.snapshot()
    def restore(self,s):self.events,self.checkpoints,self.exports,self.retentions,self.source_events,out=s;self.outbox.restore(out)
class _Stream:
    def __init__(self,s):self.s=s
    def next_sequence(self,t):
        values=self.s.events.get(t,[]);return len(values)+1,(values[-1].current_hash if values else ZERO_HASH)
    def append(self,v):
        self.s._fail("event"); values=self.s.events.setdefault(v.tenant_id,[])
        if v.sequence!=len(values)+1:raise Conflict("audit_sequence_conflict","audit sequence is not monotonic")
        values.append(v);return v
    def list(self,t,limit,cursor):
        values=self.s.events.get(t,[]);start=int(cursor or 0);items=tuple(values[start:start+limit]);return AuditPage(items,str(start+limit) if start+limit<len(values) else None)
    def range(self,t,first,last):return tuple(x for x in self.s.events.get(t,[]) if (first is None or x.sequence>=first) and (last is None or x.sequence<=last))
class _Checkpoints:
    def __init__(self,s):self.s=s
    def create(self,v):
        if v.checkpoint_id in self.s.checkpoints:raise Conflict("duplicate_checkpoint","checkpoint exists")
        self.s.checkpoints[v.checkpoint_id]=v;return v
    def get(self,k):return self.s.checkpoints.get(k)
class _Exports:
    def __init__(self,s):self.s=s
    def create(self,v):self.s.exports[v.export_id]=v;return v
    def get(self,k):return self.s.exports.get(k)
class _Retention:
    def __init__(self,s):self.s=s
    def put(self,v,expected):
        old=self.s.retentions.get(v.tenant_id)
        if old and old.version!=expected:raise Conflict("stale_version","audit retention version is stale")
        self.s.retentions[v.tenant_id]=v;return v
    def get(self,k):return self.s.retentions.get(k)
class _SourceEvents:
    def __init__(self,s):self.s=s
    def get(self,k):return self.s.source_events.get(k)
    def put(self,event_id,fingerprint,result):
        if event_id in self.s.source_events:raise Conflict("source_event_conflict","source event ID already exists")
        self.s.source_events[event_id]=(fingerprint,result)
class InMemoryAuditUnitOfWork:
    def __init__(self,s):self.s=s;self.stream=_Stream(s);self.checkpoints=_Checkpoints(s);self.exports=_Exports(s);self.retention=_Retention(s);self.source_events=_SourceEvents(s);self.outbox=s.outbox;self._committed=False
    def __enter__(self):self.s.lock.acquire();self._snapshot=self.s.snapshot();return self
    def commit(self):self._committed=True
    def __exit__(self,*args):
        if not self._committed:self.s.restore(self._snapshot)
        self.s.lock.release()
class InMemoryAuditUnitOfWorkFactory:
    def __init__(self,s):self.s=s
    def __call__(self):return InMemoryAuditUnitOfWork(self.s)
