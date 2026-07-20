import unittest

from services.ai_gateway.providers import AzureOpenAIAdapter, OpenAIAdapter, ProviderRequest
from services.ai_gateway.secrets import MappingSecretManager, SecretError


class CaptureTransport:
    def __init__(self) -> None:
        self.calls = []

    def post(self, url, headers, body, timeout_seconds):
        self.calls.append((url, headers, body, timeout_seconds))
        return {"id": "provider-id", "output_text": "result"}


class ProviderAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.secrets = MappingSecretManager({"env://TEST_KEY": "credential-value"})
        self.transport = CaptureTransport()
        self.request = ProviderRequest("request-1", "tenant-1", "model-1", "private prompt")

    def test_azure_adapter_resolves_secret_and_forwards_timeout(self) -> None:
        adapter = AzureOpenAIAdapter("azure-1", "https://azure.example", "deployment", "version", "env://TEST_KEY", self.secrets, self.transport)
        response = adapter.execute(self.request, 4.5)
        url, headers, body, timeout = self.transport.calls[0]
        self.assertIn("/openai/deployments/deployment/responses", url)
        self.assertEqual(headers["api-key"], "credential-value")
        self.assertFalse(body["store"])
        self.assertEqual(timeout, 4.5)
        self.assertEqual(response.output_text, "result")

    def test_openai_adapter_uses_bearer_secret(self) -> None:
        adapter = OpenAIAdapter("openai-1", "https://api.openai.example", "env://TEST_KEY", self.secrets, self.transport)
        adapter.execute(self.request, 3)
        _, headers, _, _ = self.transport.calls[0]
        self.assertEqual(headers["Authorization"], "Bearer credential-value")

    def test_missing_secret_fails_closed(self) -> None:
        adapter = OpenAIAdapter("openai-1", "https://api.openai.example", "env://MISSING", self.secrets, self.transport)
        with self.assertRaises(SecretError):
            adapter.execute(self.request, 3)


if __name__ == "__main__":
    unittest.main()
