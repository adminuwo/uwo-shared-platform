import unittest

from services.ai_gateway.providers import AzureOpenAIAdapter, OpenAIAdapter, ProviderError, ProviderRequest
from services.ai_gateway.secrets import MappingSecretManager, SecretError


def message(*content):
    return {
        "id": "msg_123",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": list(content),
    }


def output_text(text):
    return {"type": "output_text", "text": text, "annotations": []}


def raw_response(*output, response_id="resp_123", status="completed"):
    return {
        "id": response_id,
        "object": "response",
        "created_at": 1_700_000_000,
        "status": status,
        "error": None,
        "incomplete_details": None,
        "output": list(output),
    }


class CaptureTransport:
    def __init__(self, response=None) -> None:
        self.calls = []
        self.response = response if response is not None else raw_response(message(output_text("result")))

    def post(self, url, headers, body, timeout_seconds):
        self.calls.append((url, headers, body, timeout_seconds))
        return self.response


class ProviderAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.secrets = MappingSecretManager({"env://TEST_KEY": "credential-value"})
        self.request = ProviderRequest("request-1", "tenant-1", "uwo-general-v1", "private prompt")
        self.model_map = {"uwo-general-v1": "provider-general-model", "uwo-legal-v1": "provider-legal-model"}

    def openai(self, response):
        return OpenAIAdapter("openai-1", "https://openai.example", self.model_map, "env://TEST_KEY", self.secrets, CaptureTransport(response))

    def test_normal_text_response_and_provider_id_propagation(self) -> None:
        response = self.openai(raw_response(message(output_text("normal result")), response_id="resp_preserved")).execute(self.request, 3)
        self.assertEqual(response.output_text, "normal result")
        self.assertEqual(response.provider_request_id, "resp_preserved")

    def test_multiple_output_text_elements_and_messages_are_aggregated(self) -> None:
        raw = raw_response(
            {"id": "reasoning_1", "type": "reasoning", "summary": []},
            message(output_text("first "), output_text("second")),
            message(output_text(" and third")),
        )
        response = self.openai(raw).execute(self.request, 3)
        self.assertEqual(response.output_text, "first second and third")

    def test_refusal_response_fails_closed(self) -> None:
        raw = raw_response(message({"type": "refusal", "refusal": "cannot comply"}), response_id="resp_refused")
        with self.assertRaises(ProviderError) as caught:
            self.openai(raw).execute(self.request, 3)
        self.assertEqual(caught.exception.code, "provider_refusal")
        self.assertEqual(caught.exception.provider_response_id, "resp_refused")

    def test_empty_output_fails_closed(self) -> None:
        with self.assertRaises(ProviderError) as caught:
            self.openai(raw_response()).execute(self.request, 3)
        self.assertEqual(caught.exception.code, "missing_output")

    def test_missing_output_fails_closed(self) -> None:
        raw = raw_response(message(output_text("unused")))
        del raw["output"]
        with self.assertRaises(ProviderError) as caught:
            self.openai(raw).execute(self.request, 3)
        self.assertEqual(caught.exception.code, "missing_output")

    def test_malformed_provider_response_fails_closed(self) -> None:
        raw = raw_response(message({"type": "output_text", "annotations": []}))
        with self.assertRaises(ProviderError) as caught:
            self.openai(raw).execute(self.request, 3)
        self.assertEqual(caught.exception.code, "malformed_response")

    def test_incomplete_response_fails_closed_and_preserves_id(self) -> None:
        raw = raw_response(response_id="resp_incomplete", status="incomplete")
        raw["incomplete_details"] = {"reason": "max_output_tokens"}
        with self.assertRaises(ProviderError) as caught:
            self.openai(raw).execute(self.request, 3)
        self.assertEqual(caught.exception.code, "incomplete_response")
        self.assertEqual(caught.exception.provider_response_id, "resp_incomplete")

    def test_failed_response_fails_closed_and_preserves_id(self) -> None:
        raw = raw_response(response_id="resp_failed", status="failed")
        raw["error"] = {"code": "server_error", "message": "provider failed"}
        with self.assertRaises(ProviderError) as caught:
            self.openai(raw).execute(self.request, 3)
        self.assertEqual(caught.exception.code, "provider_response_error")
        self.assertEqual(caught.exception.provider_response_id, "resp_failed")

    def test_azure_uses_v1_url_deployment_model_api_key_and_request_id(self) -> None:
        transport = CaptureTransport()
        adapter = AzureOpenAIAdapter("azure-1", "https://azure.example", self.model_map, "env://TEST_KEY", self.secrets, transport)
        adapter.execute(self.request, 4.5)
        url, headers, body, timeout = transport.calls[0]
        self.assertEqual(url, "https://azure.example/openai/v1/responses")
        self.assertEqual(body["model"], "provider-general-model")
        self.assertEqual(headers["api-key"], "credential-value")
        self.assertEqual(headers["x-ms-client-request-id"], "request-1")
        self.assertFalse(body["store"])
        self.assertEqual(timeout, 4.5)

    def test_azure_selects_legal_deployment_from_shared_alias(self) -> None:
        transport = CaptureTransport()
        adapter = AzureOpenAIAdapter("azure-1", "https://azure.example", self.model_map, "env://TEST_KEY", self.secrets, transport)
        legal_request = ProviderRequest("request-legal", "tenant-1", "uwo-legal-v1", "legal prompt")
        adapter.execute(legal_request, 3)
        self.assertEqual(transport.calls[0][2]["model"], "provider-legal-model")

    def test_openai_uses_v1_url_and_bearer_secret(self) -> None:
        transport = CaptureTransport()
        adapter = OpenAIAdapter("openai-1", "https://openai.example", self.model_map, "env://TEST_KEY", self.secrets, transport)
        adapter.execute(self.request, 3)
        url, headers, body, _ = transport.calls[0]
        self.assertEqual(url, "https://openai.example/v1/responses")
        self.assertEqual(headers["Authorization"], "Bearer credential-value")
        self.assertEqual(headers["X-Client-Request-Id"], "request-1")
        self.assertEqual(body["model"], "provider-general-model")

    def test_openai_selects_legal_provider_model_from_shared_alias(self) -> None:
        transport = CaptureTransport()
        adapter = OpenAIAdapter("openai-1", "https://openai.example", self.model_map, "env://TEST_KEY", self.secrets, transport)
        legal_request = ProviderRequest("request-legal", "tenant-1", "uwo-legal-v1", "legal prompt")
        adapter.execute(legal_request, 3)
        self.assertEqual(transport.calls[0][2]["model"], "provider-legal-model")

    def test_unmapped_alias_fails_before_transport_call(self) -> None:
        transport = CaptureTransport()
        adapter = OpenAIAdapter("openai-1", "https://openai.example", {}, "env://MISSING", MappingSecretManager({}), transport)
        with self.assertRaises(ProviderError) as caught:
            adapter.execute(self.request, 3)
        self.assertEqual(caught.exception.code, "unmapped_model")
        self.assertEqual(transport.calls, [])

    def test_missing_secret_fails_closed(self) -> None:
        adapter = OpenAIAdapter("openai-1", "https://openai.example", self.model_map, "env://MISSING", self.secrets, CaptureTransport())
        with self.assertRaises(SecretError):
            adapter.execute(self.request, 3)


if __name__ == "__main__":
    unittest.main()
