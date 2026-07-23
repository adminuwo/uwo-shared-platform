"""Append-only hash-chain audit application service."""

from __future__ import annotations

import hashlib
from typing import Any, Callable, Mapping

from packages.contracts import (
    AuditCheckpoint, AuditExportManifest, AuditIntegrityProof, AuditRetentionPolicy,
    DurableAuditEvent, Permission, VerifiedSubjectIdentity, contract_fingerprint,
    contract_json, utc_now,
)
from services.data_service_common import (
    AuthorizationDenied, AuditSink, Conflict, DataServiceAuthorizer, InvalidRequest,
    PlatformEvent, PolicyViolation, ResourceNotFound, deterministic_id,
)

from .repositories import UnitOfWorkFactory

ZERO_HASH = "0" * 64
AUDIT_SOURCE_KEYS = frozenset({"resource_id", "reason_code", "provider_id", "region", "product", "permission", "pseudonymous_subject_id", "status"})


def _event_hash(tenant_id, sequence, action, outcome, occurred_at, request_id, actor_subject, attributes, previous_hash):
    payload = {"tenant_id": tenant_id, "sequence": sequence, "action": action, "outcome": outcome, "occurred_at": occurred_at, "request_id": request_id, "actor_subject": actor_subject, "attributes": attributes, "previous_hash": previous_hash, "schema_version": "1"}
    return hashlib.sha256(contract_json(payload).encode()).hexdigest()


class PlatformAuditService:
    def __init__(self, uow: UnitOfWorkFactory, authorizer: DataServiceAuthorizer, audit: AuditSink, clock: Callable[[], str] = utc_now) -> None:
        self._uow = uow
        self._auth = authorizer
        self._audit = audit
        self._clock = clock

    def _append_unchecked(self, tx, tenant_id, action, outcome, request_id, attributes, actor_subject, occurred_at):
        sequence, previous = tx.stream.next_sequence(tenant_id)
        current = _event_hash(tenant_id, sequence, action, outcome, occurred_at, request_id, actor_subject, attributes, previous)
        event_id = deterministic_id("audit", tenant_id, sequence, current)
        value = DurableAuditEvent(event_id, tenant_id, sequence, action, outcome, occurred_at, request_id, actor_subject, attributes, previous, current, True)
        return tx.stream.append(value)

    def append(self, identity, tenant_id, action, outcome, request_id, attributes: Mapping[str, Any], actor_subject=None):
        self._auth.require_executor(identity, tenant_id, allow_suspended=True)
        if actor_subject is not None and actor_subject != identity.subject:
            raise AuthorizationDenied("actor_provenance_mismatch", "caller cannot assign another actor identity")
        with self._uow() as tx:
            result = self._append_unchecked(tx, tenant_id, action, outcome, request_id, attributes, identity.subject, self._clock())
            tx.commit()
            return result

    def append_source_event(self, identity: VerifiedSubjectIdentity, event: PlatformEvent):
        self._auth.require_executor(identity, event.tenant_id, allow_suspended=True)
        fingerprint = contract_fingerprint(event)
        attributes = {key: value for key, value in event.attributes.items() if key in AUDIT_SOURCE_KEYS}
        with self._uow() as tx:
            existing = tx.source_events.get(event.event_id)
            if existing is not None:
                if existing[0] != fingerprint:
                    raise Conflict("source_event_conflict", "source event ID was reused with different content")
                tx.commit()
                return existing[1]
            result = self._append_unchecked(
                tx,
                event.tenant_id,
                event.event_type,
                "recorded",
                event.request_id,
                attributes,
                identity.subject,
                event.occurred_at,
            )
            tx.source_events.put(event.event_id, fingerprint, result)
            tx.commit()
            return result

    def list(self, identity, tenant_id, limit=50, cursor=None):
        self._auth.require(identity, tenant_id, Permission.AUDIT_READ, allow_suspended=True)
        if not 1 <= limit <= 100:
            raise InvalidRequest("invalid_pagination", "limit must be 1 to 100")
        with self._uow() as tx:
            page = tx.stream.list(tenant_id, limit, cursor)
            tx.commit()
            return page

    def _events(self, tenant_id):
        with self._uow() as tx:
            events = tx.stream.range(tenant_id, None, None)
            tx.commit()
            return events

    def _verify_events(self, tenant_id, events, through_sequence=None):
        expected_sequence = 1
        previous = ZERO_HASH
        selected = events if through_sequence is None else tuple(event for event in events if event.sequence <= through_sequence)
        for event in selected:
            if event.tenant_id != tenant_id or event.sequence != expected_sequence:
                return AuditIntegrityProof(tenant_id, False, len(selected), expected_sequence, self._clock())
            expected = _event_hash(event.tenant_id, event.sequence, event.action, event.outcome, event.occurred_at, event.request_id, event.actor_subject, event.attributes, previous)
            if event.previous_hash != previous or event.current_hash != expected:
                return AuditIntegrityProof(tenant_id, False, len(selected), event.sequence, self._clock())
            previous = event.current_hash
            expected_sequence += 1
        if through_sequence is not None and len(selected) != through_sequence:
            return AuditIntegrityProof(tenant_id, False, len(selected), expected_sequence, self._clock())
        return AuditIntegrityProof(tenant_id, True, len(selected), None, self._clock())

    def verify(self, identity, tenant_id):
        self._auth.require(identity, tenant_id, Permission.AUDIT_VERIFY, allow_suspended=True)
        return self._verify_events(tenant_id, self._events(tenant_id))

    def _verify_unchecked(self, tenant_id):
        return self._verify_events(tenant_id, self._events(tenant_id))

    def checkpoint(self, identity, tenant_id, request_id):
        self._auth.require(identity, tenant_id, Permission.AUDIT_VERIFY, allow_suspended=True)
        events = self._events(tenant_id)
        proof = self._verify_events(tenant_id, events)
        if not proof.valid:
            raise PolicyViolation("audit_integrity_failure", "audit hash chain verification failed")
        if not events:
            raise ResourceNotFound("empty_audit_stream", "audit stream is empty")
        last = events[-1]
        value = AuditCheckpoint(deterministic_id("checkpoint", tenant_id, last.sequence, last.current_hash), tenant_id, last.sequence, last.current_hash, self._clock())
        with self._uow() as tx:
            result = tx.checkpoints.create(value)
            tx.commit()
            return result

    def verify_checkpoint(self, identity, tenant_id, checkpoint_id):
        self._auth.require(identity, tenant_id, Permission.AUDIT_VERIFY, allow_suspended=True)
        with self._uow() as tx:
            checkpoint = tx.checkpoints.get(checkpoint_id)
            events = tx.stream.range(tenant_id, None, None)
            tx.commit()
        if checkpoint is None or checkpoint.tenant_id != tenant_id:
            raise ResourceNotFound("unknown_checkpoint", "checkpoint does not exist")
        proof = self._verify_events(tenant_id, events, checkpoint.through_sequence)
        if not proof.valid:
            return False
        matching = [event for event in events if event.sequence == checkpoint.through_sequence]
        return len(matching) == 1 and matching[0].tenant_id == checkpoint.tenant_id and matching[0].current_hash == checkpoint.event_hash

    def export(self, identity, tenant_id, request_id, first_sequence=None, last_sequence=None):
        self._auth.require(identity, tenant_id, Permission.AUDIT_EXPORT, allow_suspended=True)
        all_events = self._events(tenant_id)
        if not self._verify_events(tenant_id, all_events).valid:
            raise PolicyViolation("audit_integrity_failure", "cannot export an invalid audit stream")
        if first_sequence is not None and (not isinstance(first_sequence, int) or first_sequence < 1):
            raise InvalidRequest("invalid_audit_range", "first sequence must be positive")
        if last_sequence is not None and (not isinstance(last_sequence, int) or last_sequence < 1):
            raise InvalidRequest("invalid_audit_range", "last sequence must be positive")
        if first_sequence is not None and last_sequence is not None and first_sequence > last_sequence:
            raise InvalidRequest("invalid_audit_range", "first sequence cannot exceed last sequence")
        events = tuple(event for event in all_events if (first_sequence is None or event.sequence >= first_sequence) and (last_sequence is None or event.sequence <= last_sequence))
        if first_sequence is not None and (not events or events[0].sequence != first_sequence):
            raise ResourceNotFound("audit_range_missing", "requested audit range boundary does not exist")
        if last_sequence is not None and (not events or events[-1].sequence != last_sequence):
            raise ResourceNotFound("audit_range_missing", "requested audit range boundary does not exist")
        if any(right.sequence != left.sequence + 1 for left, right in zip(events, events[1:])):
            raise PolicyViolation("audit_integrity_failure", "requested audit range is not contiguous")
        digest = hashlib.sha256(contract_json(events).encode()).hexdigest()
        first = events[0].sequence if events else 0
        last = events[-1].sequence if events else 0
        manifest = AuditExportManifest(deterministic_id("audit-export", tenant_id, first, last, digest), tenant_id, first, last, len(events), digest, self._clock())
        with self._uow() as tx:
            tx.exports.create(manifest)
            tx.commit()
        return manifest, events

    @staticmethod
    def verify_export(manifest: AuditExportManifest, events: tuple[DurableAuditEvent, ...]) -> bool:
        if len(events) != manifest.event_count:
            return False
        if not events:
            return (
                manifest.first_sequence == 0
                and manifest.last_sequence == 0
                and hashlib.sha256(contract_json(events).encode()).hexdigest() == manifest.integrity_hash
            )
        if (
            events[0].sequence != manifest.first_sequence
            or events[-1].sequence != manifest.last_sequence
            or manifest.last_sequence - manifest.first_sequence + 1 != manifest.event_count
        ):
            return False
        previous = ZERO_HASH if manifest.first_sequence == 1 else events[0].previous_hash
        expected_sequence = manifest.first_sequence
        for event in events:
            if event.tenant_id != manifest.tenant_id or event.sequence != expected_sequence:
                return False
            expected_hash = _event_hash(
                event.tenant_id,
                event.sequence,
                event.action,
                event.outcome,
                event.occurred_at,
                event.request_id,
                event.actor_subject,
                event.attributes,
                previous,
            )
            if event.previous_hash != previous or event.current_hash != expected_hash:
                return False
            previous = event.current_hash
            expected_sequence += 1
        return hashlib.sha256(contract_json(events).encode()).hexdigest() == manifest.integrity_hash

    def set_retention(self, identity, tenant_id, retain_until, legal_hold, expected_version, request_id):
        self._auth.require_platform_admin(identity)
        with self._uow() as tx:
            old = tx.retention.get(tenant_id)
            value = AuditRetentionPolicy(deterministic_id("audit-retention", tenant_id), tenant_id, retain_until, legal_hold, old.created_at if old else self._clock(), old.version + 1 if old else 1)
            result = tx.retention.put(value, expected_version)
            tx.commit()
            return result


class DurableAuditEventPublisher:
    """Idempotent provider-neutral bridge from platform events into audit."""

    def __init__(self, service: PlatformAuditService, identity: VerifiedSubjectIdentity) -> None:
        self._service = service
        self._identity = identity

    def publish(self, event: PlatformEvent) -> None:
        self._service.append_source_event(self._identity, event)
