"""Safe provider-neutral parsing for raw OpenAI Responses API JSON."""

from __future__ import annotations

from typing import Any, Mapping

from .base import ProviderError, ProviderResponse


def _response_id(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("id")
    return value if isinstance(value, str) and value else None


def parse_responses_payload(payload: Mapping[str, Any]) -> ProviderResponse:
    """Extract all output text or fail closed on unsafe/invalid response states."""

    if not isinstance(payload, Mapping):
        raise ProviderError("provider response must be a JSON object", code="malformed_response")
    response_id = _response_id(payload)
    if response_id is None:
        raise ProviderError("provider response id is missing or invalid", code="malformed_response")
    status = payload.get("status")
    if status == "incomplete":
        details = payload.get("incomplete_details")
        reason = details.get("reason") if isinstance(details, Mapping) else None
        suffix = f": {reason}" if isinstance(reason, str) and reason else ""
        raise ProviderError(f"provider response is incomplete{suffix}", code="incomplete_response", provider_response_id=response_id)
    if payload.get("error") is not None:
        raise ProviderError("provider response contains an error", code="provider_response_error", provider_response_id=response_id)
    if status != "completed":
        raise ProviderError("provider response status is missing or invalid", code="malformed_response", provider_response_id=response_id)

    if "output" not in payload or payload.get("output") is None:
        raise ProviderError("provider response contains no output", code="missing_output", provider_response_id=response_id)
    output = payload.get("output")
    if not isinstance(output, list):
        raise ProviderError("provider response output must be a list", code="malformed_response", provider_response_id=response_id)

    text_parts: list[str] = []
    refused = False
    for item in output:
        if not isinstance(item, Mapping):
            raise ProviderError("provider response output item is malformed", code="malformed_response", provider_response_id=response_id)
        item_type = item.get("type")
        if not isinstance(item_type, str):
            raise ProviderError("provider response output type is malformed", code="malformed_response", provider_response_id=response_id)
        if item_type != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            raise ProviderError("provider response message content must be a list", code="malformed_response", provider_response_id=response_id)
        for part in content:
            if not isinstance(part, Mapping) or not isinstance(part.get("type"), str):
                raise ProviderError("provider response content item is malformed", code="malformed_response", provider_response_id=response_id)
            if part["type"] == "refusal":
                refused = True
            elif part["type"] == "output_text":
                text = part.get("text")
                if not isinstance(text, str):
                    raise ProviderError("provider output_text content is malformed", code="malformed_response", provider_response_id=response_id)
                text_parts.append(text)

    if refused:
        raise ProviderError("provider refused the request", fallback_allowed=False, code="provider_refusal", provider_response_id=response_id)
    output_text = "".join(text_parts)
    if not output_text:
        raise ProviderError("provider response contains no output text", code="missing_output", provider_response_id=response_id)
    return ProviderResponse(provider_request_id=response_id, output_text=output_text)
