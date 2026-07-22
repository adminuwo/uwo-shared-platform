"""Thread-safe rollback-capable analytics test repositories."""
from __future__ import annotations
from threading import RLock
from services.data_service_common import Conflict,InMemoryOutbox,RepositoryIntegrityError
class InMemoryAnalyticsState:
    def __init__(self): self.events={}; self.snapshots={}; self.idempotency={}; self.outbox=InMemoryOutbox(); self.lock=RLock(); self.fail_next=None
    def _fail(self,op):
        if self.fail_next==op: self.fail_next=None; raise RepositoryIntegrityError(f"injected {op} failure")
    def snapshot(self): return dict(self.events),dict(self.snapshots),dict(self.idempotency),self.outbox.snapshot()
    def restore(self,s): self.events,self.snapshots,self.idempotency,out=s; self.outbox.restore(out)
class _Events:
    def __init__(self,s):self.s=s
    def append(self,v):
        self.s._fail("event"); old=self.s.events.get(v.event_id)
        if old is not None and old!=v: raise Conflict("event_id_conflict","event ID identifies different content")
        self.s.events[v.event_id]=v; return old or v
    def get(self,k): return self.s.events.get(k)
    def range(self,t,start,end): return tuple(sorted((x for x in self.s.events.values() if x.tenant_id==t and start<=x.occurred_at<end),key=lambda x:(x.occurred_at,x.event_id)))
class _Snapshots:
    def __init__(self,s):self.s=s
    def put(self,v):
        old=self.s.snapshots.get(v.snapshot_id)
        if old is not None and old!=v: raise Conflict("snapshot_conflict","snapshot is immutable")
        self.s.snapshots[v.snapshot_id]=v; return old or v
    def get(self,k): return self.s.snapshots.get(k)
class _Idempotency:
    def __init__(self,s):self.s=s
    def get(self,scope,key): return self.s.idempotency.get((scope,key))
    def put(self,scope,key,fp,result):
        self.s._fail("idempotency"); self.s.idempotency[(scope,key)]=(fp,result)
class InMemoryAnalyticsUnitOfWork:
    def __init__(self,s): self.s=s; self.events=_Events(s); self.snapshots=_Snapshots(s); self.idempotency=_Idempotency(s); self.outbox=s.outbox; self._committed=False
    def __enter__(self): self.s.lock.acquire(); self._snapshot=self.s.snapshot(); return self
    def commit(self): self._committed=True
    def __exit__(self,*args):
        if not self._committed:self.s.restore(self._snapshot)
        self.s.lock.release()
class InMemoryAnalyticsUnitOfWorkFactory:
    def __init__(self,s):self.s=s
    def __call__(self):return InMemoryAnalyticsUnitOfWork(self.s)
