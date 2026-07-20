"""OpenAI adapter scaffold."""

from __future__ import annotations

from ..secrets import SecretManager
from .base import JsonTransport, ProviderError, ProviderRequest, ProviderResponse
from .transport import UrllibJsonTransport


class OpenAIAdapter:
    def __init__(self, provider_id: str, endpoint: str, secret_ref: str, secrets: SecretManager, transport: JsonTransport | None = None) -> None:
        self.provider_id = provider_id
        self._url = f"{endpoint.rstrip('/')}/v1/responses"
        self._secret_ref = secret_ref
        self._secrets = secrets
        self._transport = transport or UrllibJsonTransport()

    def execute(self, request: ProviderRequest, timeout_seconds: float) -> ProviderResponse:
        payload = self._transport.post(
            self._url,
            {"Content-Type": "application/json", "Authorization": f"Bearer {self._secrets.get_secret(self._secret_ref)}", "X-Client-Request-Id": request.request_id},
            {"model": request.model, "input": request.prompt, "store": False},
            timeout_seconds,
        )
        try:
            return ProviderResponse(provider_request_id=payload.get("id"), output_text=str(payload["output_text"]))
        except (KeyError, TypeError) as exc:
            raise ProviderError("OpenAI response omitted output_text") from exc
