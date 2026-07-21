"""Structured, redacted administration audit events."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Callable, Protocol

from packages.contracts import utc_now


@dataclass(frozen=True)
class ControlPlaneAuditEvent:
    timestamp: str
    event_type: str
    request_id: str
    outcome: str
    actor_subject: str | None = None
    tenant_id: str | None = None
    target_subject: str | None = None
    resource_id: str | None = None
    reason_code: str | None = None


class AuditSink(Protocol):
    def emit(self, event: ControlPlaneAuditEvent) -> None: ...


class JsonAuditSink:
    def __init__(self, writer: Callable[[str], None] = print) -> None:
        self._writer = writer

    def emit(self, event: ControlPlaneAuditEvent) -> None:
        self._writer(json.dumps({key: value for key, value in asdict(event).items() if value is not None}, separators=(",", ":")))


def audit_event(event_type: str, request_id: str, outcome: str, **fields: str | None) -> ControlPlaneAuditEvent:
    return ControlPlaneAuditEvent(utc_now(), event_type, request_id, outcome, **fields)
