"""OpenAI adapter scaffold."""

from __future__ import annotations

from typing import Mapping

from ..secrets import SecretManager
from .base import JsonTransport, ProviderRequest, ProviderResponse, resolve_provider_model
from .responses import parse_responses_payload
from .transport import UrllibJsonTransport


class OpenAIAdapter:
    def __init__(self, provider_id: str, endpoint: str, model_map: Mapping[str, str], secret_ref: str, secrets: SecretManager, transport: JsonTransport | None = None) -> None:
        self.provider_id = provider_id
        self._url = f"{endpoint.rstrip('/')}/v1/responses"
        self._model_map = dict(model_map)
        self._secret_ref = secret_ref
        self._secrets = secrets
        self._transport = transport or UrllibJsonTransport()

    def execute(self, request: ProviderRequest, timeout_seconds: float) -> ProviderResponse:
        provider_model = resolve_provider_model(self._model_map, request.model)
        payload = self._transport.post(
            self._url,
            {"Content-Type": "application/json", "Authorization": f"Bearer {self._secrets.get_secret(self._secret_ref)}", "X-Client-Request-Id": request.request_id},
            {"model": provider_model, "input": request.prompt, "store": False},
            timeout_seconds,
        )
        return parse_responses_payload(payload)
