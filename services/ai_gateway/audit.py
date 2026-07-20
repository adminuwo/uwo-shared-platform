"""Structured audit events with an allowlisted, prompt-free schema."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol


@dataclass(frozen=True)
class AuditEvent:
    timestamp: str
    event_type: str
    request_id: str
    outcome: str
    tenant_id: str | None = None
    subject: str | None = None
    product: str | None = None
    model: str | None = None
    provider: str | None = None
    reason_code: str | None = None


class AuditSink(Protocol):
    def emit(self, event: AuditEvent) -> None: ...


class JsonAuditSink:
    def __init__(self, writer: Callable[[str], None] = print) -> None:
        self._writer = writer

    def emit(self, event: AuditEvent) -> None:
        self._writer(json.dumps({key: value for key, value in asdict(event).items() if value is not None}, separators=(",", ":")))


def audit_event(event_type: str, request_id: str, outcome: str, **fields: str | None) -> AuditEvent:
    return AuditEvent(datetime.now(timezone.utc).isoformat(), event_type, request_id, outcome, **fields)
