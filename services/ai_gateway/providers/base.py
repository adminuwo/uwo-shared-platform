"""Provider-neutral execution contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False, fallback_allowed: bool = True, code: str = "provider_error", provider_response_id: str | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.fallback_allowed = fallback_allowed
        self.code = code
        self.provider_response_id = provider_response_id


class ProviderTimeout(ProviderError):
    def __init__(self, message: str = "provider request timed out") -> None:
        super().__init__(message, retryable=True)


def resolve_provider_model(model_map: Mapping[str, str], alias: str) -> str:
    try:
        provider_model = model_map[alias]
    except KeyError as exc:
        raise ProviderError("provider model mapping is unavailable", fallback_allowed=False, code="unmapped_model") from exc
    if not isinstance(provider_model, str) or not provider_model.strip():
        raise ProviderError("provider model mapping is invalid", fallback_allowed=False, code="unmapped_model")
    return provider_model


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
    provider_model_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class JsonTransport(Protocol):
    def post(self, url: str, headers: Mapping[str, str], body: Mapping[str, Any], timeout_seconds: float) -> Mapping[str, Any]: ...


class ProviderAdapter(Protocol):
    provider_id: str

    def execute(self, request: ProviderRequest, timeout_seconds: float) -> ProviderResponse: ...
