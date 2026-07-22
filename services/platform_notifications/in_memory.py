"""Thread-safe rollback-capable notification test repositories."""
from __future__ import annotations
from threading import RLock
from packages.contracts import *
from services.data_service_common import Conflict, InMemoryOutbox, RepositoryIntegrityError
from .repositories import NotificationPage, ProviderAcceptance

class FakeNotificationProvider:
    def __init__(self,outcomes:tuple[ProviderAcceptance,...]=()) -> None: self._outcomes=list(outcomes); self.calls:list[str]=[]
    def deliver(self,notification,template):
        self.calls.append(notification.notification_id)
        return self._outcomes.pop(0) if self._outcomes else ProviderAcceptance(True,False,f"provider-{notification.notification_id}",None)

class StaticWebhookAllowlist:
    def __init__(self,allowed:frozenset[tuple[str,str]])->None: self.allowed=allowed
    def permits(self,tenant_id,destination_reference): return (tenant_id,destination_reference) in self.allowed

class _FailingOutbox(InMemoryOutbox):
    def __init__(self,state): super().__init__(); self.state=state
    def enqueue(self,record): self.state._fail("outbox"); return super().enqueue(record)

class InMemoryNotificationState:
    def __init__(self)->None:
        self.templates={}; self.template_versions={}; self.notifications={}; self.attempts={}; self.preferences={}; self.dead_letters={}; self.lock=RLock(); self.fail_next=None; self.outbox=_FailingOutbox(self)
    def _fail(self,op):
        if self.fail_next==op: self.fail_next=None; raise RepositoryIntegrityError(f"injected {op} failure")
    def snapshot(self): return tuple(dict(x) for x in (self.templates,self.template_versions,self.notifications,self.attempts,self.preferences,self.dead_letters))+(self.outbox.snapshot(),)
    def restore(self,s): self.templates,self.template_versions,self.notifications,self.attempts,self.preferences,self.dead_letters,out=s; self.outbox.restore(out)

class _Templates:
    def __init__(self,s): self.s=s
    def create(self,v):
        if v.template_id in self.s.templates: raise Conflict("duplicate_template","template exists")
        self.s.templates[v.template_id]=v; return v
    def get(self,k): return self.s.templates.get(k)
    def update(self,v,expected_version):
        c=self.s.templates.get(v.template_id)
        if c is None: raise Conflict("unknown_template","template does not exist")
        if c.version!=expected_version: raise Conflict("stale_version","template version is stale")
        self.s.templates[v.template_id]=v; return v
    def append_version(self,v):
        k=(v.template_id,v.version_number)
        if k in self.s.template_versions: raise Conflict("duplicate_template_version","template version exists")
        self.s.template_versions[k]=v; return v
    def get_version(self,t,n): return self.s.template_versions.get((t,n))
class _Notifications:
    def __init__(self,s): self.s=s
    def create(self,v):
        self.s._fail("notification")
        if v.notification_id in self.s.notifications: raise Conflict("duplicate_notification","notification exists")
        if self.get_by_dedup(v.tenant_id,v.deduplication_key): raise Conflict("duplicate_enqueue","deduplication key exists")
        self.s.notifications[v.notification_id]=v; return v
    def get(self,k): return self.s.notifications.get(k)
    def update(self,v,expected_version):
        c=self.s.notifications.get(v.notification_id)
        if c is None: raise Conflict("unknown_notification","notification does not exist")
        if c.version!=expected_version: raise Conflict("stale_version","notification version is stale")
        self.s.notifications[v.notification_id]=v; return v
    def list(self,tenant_id,limit,cursor):
        vals=sorted((x for x in self.s.notifications.values() if x.tenant_id==tenant_id),key=lambda x:x.notification_id); start=int(cursor or 0); items=tuple(vals[start:start+limit]); return NotificationPage(items,str(start+limit) if start+limit<len(vals) else None)
    def get_by_dedup(self,tenant_id,key): return next((x for x in self.s.notifications.values() if x.tenant_id==tenant_id and x.deduplication_key==key),None)
class _Attempts:
    def __init__(self,s): self.s=s
    def append(self,v): self.s.attempts[v.attempt_id]=v; return v
    def list(self,k): return tuple(sorted((x for x in self.s.attempts.values() if x.notification_id==k),key=lambda x:x.attempt_number))
class _Preferences:
    def __init__(self,s): self.s=s
    def put(self,v,expected_version):
        key=(v.tenant_id,v.subject_reference,v.channel.value); old=self.s.preferences.get(key)
        if old and old.version!=expected_version: raise Conflict("stale_version","preference version is stale")
        self.s.preferences[key]=v; return v
    def get(self,t,s,c): return self.s.preferences.get((t,s,c))
class _DeadLetters:
    def __init__(self,s): self.s=s
    def create(self,v): self.s.dead_letters[v.dead_letter_id]=v; return v
    def get(self,k): return self.s.dead_letters.get(k)

class InMemoryNotificationUnitOfWork:
    def __init__(self,s): self.s=s; self.templates=_Templates(s); self.notifications=_Notifications(s); self.attempts=_Attempts(s); self.preferences=_Preferences(s); self.dead_letters=_DeadLetters(s); self.outbox=s.outbox; self._committed=False
    def __enter__(self): self.s.lock.acquire(); self._snapshot=self.s.snapshot(); return self
    def commit(self): self._committed=True
    def __exit__(self,*args):
        if not self._committed: self.s.restore(self._snapshot)
        self.s.lock.release()
class InMemoryNotificationUnitOfWorkFactory:
    def __init__(self,s): self.s=s
    def __call__(self): return InMemoryNotificationUnitOfWork(self.s)
