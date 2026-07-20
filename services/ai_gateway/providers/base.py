"""Provider-neutral execution contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ProviderTimeout(ProviderError):
    def __init__(self, message: str = "provider request timed out") -> None:
        super().__init__(message, retryable=True)


@dataclass(frozen=True)
class ProviderRequest:
    request_id: str
    tenant_id: str
    model: str
    prompt: str


@dataclass(frozen=True)
class ProviderResponse:
    provider_request_id: str | None
    output_text: str


class JsonTransport(Protocol):
    def post(self, url: str, headers: Mapping[str, str], body: Mapping[str, Any], timeout_seconds: float) -> Mapping[str, Any]: ...


class ProviderAdapter(Protocol):
    provider_id: str

    def execute(self, request: ProviderRequest, timeout_seconds: float) -> ProviderResponse: ...
