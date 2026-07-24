"""Rollback-capable test-only storage repositories and fake blob store."""

from __future__ import annotations
from dataclasses import replace
from threading import RLock
from typing import Any
from packages.contracts import LegalHold, MalwareScanResult, ObjectVersion, RetentionPolicy, StoredObject, UploadSession
from services.data_service_common import Conflict, InMemoryOutbox, RepositoryIntegrityError
from .repositories import BlobMetadata, ObjectPage

class FakeBlobStore:
    """Stores only provider metadata; never file bytes."""
    def __init__(self) -> None: self._metadata: dict[str, BlobMetadata] = {}; self._lock = RLock()
    def stage(self, storage_key: str, content_length: int, algorithm: str, digest: str) -> None:
        with self._lock: self._metadata[storage_key] = BlobMetadata(content_length, algorithm, digest)
    def stat(self, storage_key: str) -> BlobMetadata | None:
        with self._lock: return self._metadata.get(storage_key)

class _StorageOutbox(InMemoryOutbox):
    def __init__(self,state): super().__init__();self.state=state
    def enqueue(self,record): self.state._fail("outbox");return super().enqueue(record)

class InMemoryStorageRepository:
    def __init__(self) -> None:
        self.objects: dict[str, StoredObject] = {}; self.versions: dict[str, ObjectVersion] = {}; self.uploads: dict[str, UploadSession] = {}
        self.retentions: dict[str, RetentionPolicy] = {}; self.holds: dict[str, LegalHold] = {}; self.scans: dict[str, MalwareScanResult] = {}; self.idempotency: dict[tuple[tuple[str,str,str],str], tuple[str,Any]] = {}
        self.lock = RLock(); self.fail_next: str | None = None; self.outbox = _StorageOutbox(self)
    def _fail(self, operation: str) -> None:
        if self.fail_next == operation: self.fail_next = None; raise RepositoryIntegrityError(f"injected {operation} failure")
    def snapshot(self): return (dict(self.objects),dict(self.versions),dict(self.uploads),dict(self.retentions),dict(self.holds),dict(self.scans),dict(self.idempotency),self.outbox.snapshot())
    def restore(self,s): self.objects,self.versions,self.uploads,self.retentions,self.holds,self.scans,self.idempotency,outbox=s; self.outbox.restore(outbox)

class _Objects:
    def __init__(self,s): self.s=s
    def create(self,v):
        self.s._fail("object")
        if v.object_id in self.s.objects: raise Conflict("duplicate_object","object already exists")
        self.s.objects[v.object_id]=v; return v
    def get(self,k): return self.s.objects.get(k)
    def update(self,v,expected_version):
        current=self.s.objects.get(v.object_id)
        if current is None: raise Conflict("unknown_object","object does not exist")
        if current.version != expected_version: raise Conflict("stale_version","object version is stale")
        self.s.objects[v.object_id]=v; return v
    def list(self,tenant_id,limit,cursor):
        values=sorted((v for v in self.s.objects.values() if v.tenant_id==tenant_id),key=lambda x:x.object_id)
        start=int(cursor or 0); items=tuple(values[start:start+limit]); nxt=str(start+limit) if start+limit<len(values) else None; return ObjectPage(items,nxt)

class _Versions:
    def __init__(self,s): self.s=s
    def append(self,v):
        self.s._fail("version")
        if v.object_version_id in self.s.versions: raise Conflict("duplicate_object_version","object version already exists")
        if any(x.object_id==v.object_id and x.version_number==v.version_number for x in self.s.versions.values()): raise Conflict("duplicate_version_number","version number already exists")
        self.s.versions[v.object_version_id]=v; return v
    def get(self,k): return self.s.versions.get(k)
    def get_number(self,object_id,version_number): return next((x for x in self.s.versions.values() if x.object_id==object_id and x.version_number==version_number),None)
class _Scans:
    def __init__(self,s): self.s=s
    def append(self,v):
        self.s._fail("scan")
        if v.scan_result_id in self.s.scans: raise Conflict("duplicate_scan_result","scan result already exists")
        self.s.scans[v.scan_result_id]=v; return v
    def list(self,k): return tuple(sorted((x for x in self.s.scans.values() if x.object_version_id==k),key=lambda x:(x.scanned_at,x.scan_result_id)))

class _Uploads:
    def __init__(self,s): self.s=s
    def create(self,v):
        self.s._fail("upload")
        if v.upload_id in self.s.uploads: raise Conflict("duplicate_upload","upload already exists")
        self.s.uploads[v.upload_id]=v; return v
    def get(self,k): return self.s.uploads.get(k)
    def update(self,v,expected_version):
        c=self.s.uploads.get(v.upload_id)
        if c is None: raise Conflict("unknown_upload","upload does not exist")
        if c.version!=expected_version: raise Conflict("stale_version","upload version is stale")
        self.s.uploads[v.upload_id]=v; return v

class _Policies:
    def __init__(self,s): self.s=s
    def put_retention(self,v,expected_version):
        old=self.s.retentions.get(v.object_id); current=old.version if old else 0
        if current!=expected_version: raise Conflict("stale_version","retention policy version is stale")
        self.s.retentions[v.object_id]=v; return v
    def get_retention(self,k): return self.s.retentions.get(k)
    def put_hold(self,v): self.s.holds[v.object_id]=v; return v
    def get_hold(self,k): return self.s.holds.get(k)

class _Idempotency:
    def __init__(self,s): self.s=s
    def get(self,scope,key): return self.s.idempotency.get((scope,key))
    def put(self,scope,key,fingerprint,result):
        self.s._fail("idempotency"); k=(scope,key)
        if k in self.s.idempotency: raise Conflict("idempotency_conflict","idempotency record already exists")
        self.s.idempotency[k]=(fingerprint,result)

class InMemoryStorageUnitOfWork:
    def __init__(self,state): self.state=state; self.objects=_Objects(state); self.versions=_Versions(state); self.scans=_Scans(state); self.uploads=_Uploads(state); self.policies=_Policies(state); self.idempotency=_Idempotency(state); self.outbox=state.outbox; self._committed=False
    def __enter__(self): self.state.lock.acquire(); self._snapshot=self.state.snapshot(); return self
    def commit(self): self._committed=True
    def __exit__(self,*args):
        if not self._committed: self.state.restore(self._snapshot)
        self.state.lock.release()

class InMemoryStorageUnitOfWorkFactory:
    def __init__(self,state): self.state=state
    def __call__(self): return InMemoryStorageUnitOfWork(self.state)
