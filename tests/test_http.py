import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from services.ai_gateway.app import GatewayHandler, build_dependencies
from services.ai_gateway.audit import AuditEvent
from services.ai_gateway.auth import HmacBearerAuthenticator, issue_test_token
from services.ai_gateway.config import load_config
from services.ai_gateway.providers import ProviderRequest, ProviderResponse

ROOT = Path(__file__).resolve().parents[1]
SIGNING_KEY = "test-signing-key-with-at-least-32-chars"


class FakeAdapter:
    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id

    def execute(self, request: ProviderRequest, timeout_seconds: float) -> ProviderResponse:
        return ProviderResponse("provider-request-1", "safe response")


class CaptureAudit:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def emit(self, event: AuditEvent) -> None:
        self.events.append(event)


class GatewayHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        config = load_config(ROOT / "infrastructure/config/ai-gateway.json")
        cls.audit = CaptureAudit()
        adapters = {provider.id: FakeAdapter(provider.id) for provider in config.providers}
        GatewayHandler.dependencies = build_dependencies(config, HmacBearerAuthenticator(SIGNING_KEY, clock=lambda: 100), adapters, cls.audit)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def token(self, tenant: str = "tenant-demo-in") -> str:
        return issue_test_token(SIGNING_KEY, "user-123", tenant, 200)

    def post(self, path: str, body: dict, tenant: str = "tenant-demo-in", request_id: str = "req-test-123"):
        return urlopen(Request(
            f"{self.base_url}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token(tenant)}", "X-Request-ID": request_id},
            method="POST",
        ))

    @staticmethod
    def body(tenant: str = "tenant-demo-in") -> dict:
        return {"tenant_id": tenant, "product": "aisa", "model": "uwo-general-v1", "region": "in"}

    def test_health_is_public_and_returns_request_id(self) -> None:
        with urlopen(f"{self.base_url}/healthz") as response:
            payload = json.load(response)
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(response.headers["X-Request-ID"], payload["request_id"])

    def test_route_requires_authentication(self) -> None:
        request = Request(f"{self.base_url}/v1/route", data=json.dumps(self.body()).encode(), method="POST")
        with self.assertRaises(HTTPError) as caught:
            urlopen(request)
        self.assertEqual(caught.exception.code, 401)
        self.assertEqual(json.load(caught.exception)["error"]["code"], "missing_bearer_token")

    def test_route_binds_verified_tenant_and_propagates_request_id(self) -> None:
        with self.post("/v1/route", self.body(), request_id="req-route-1") as response:
            payload = json.load(response)
            self.assertEqual(payload["provider"], "azure-openai-in")
            self.assertEqual(payload["fallback"], ["openai-in"])
            self.assertEqual(response.headers["X-Request-ID"], "req-route-1")

    def test_tenant_identity_mismatch_is_forbidden(self) -> None:
        with self.assertRaises(HTTPError) as caught:
            self.post("/v1/route", self.body("tenant-demo-in"), tenant="tenant-legal-eu")
        self.assertEqual(caught.exception.code, 403)
        self.assertEqual(json.load(caught.exception)["error"]["code"], "tenant_identity_mismatch")

    def test_product_entitlement_is_enforced(self) -> None:
        body = self.body()
        body["product"] = "ai-legal-professional"
        with self.assertRaises(HTTPError) as caught:
            self.post("/v1/route", body)
        self.assertEqual(json.load(caught.exception)["error"]["code"], "product_not_entitled")

    def test_model_entitlement_is_enforced(self) -> None:
        body = self.body()
        body["model"] = "uwo-legal-v1"
        with self.assertRaises(HTTPError) as caught:
            self.post("/v1/route", body)
        self.assertEqual(json.load(caught.exception)["error"]["code"], "model_not_entitled")

    def test_execute_enforces_billing(self) -> None:
        body = self.body("tenant-block-example")
        body["prompt"] = "hello"
        with self.assertRaises(HTTPError) as caught:
            self.post("/v1/execute", body, tenant="tenant-block-example")
        self.assertEqual(caught.exception.code, 402)
        self.assertEqual(json.load(caught.exception)["error"]["code"], "credits_not_authorized")

    def test_execute_returns_provider_response_without_auditing_prompt(self) -> None:
        body = self.body()
        body["prompt"] = "sensitive prompt value"
        with self.post("/v1/execute", body, request_id="req-execute-1") as response:
            payload = json.load(response)
        self.assertEqual(payload["provider"], "azure-openai-in")
        self.assertEqual(payload["output_text"], "safe response")
        serialized_events = json.dumps([event.__dict__ for event in self.audit.events])
        self.assertNotIn(body["prompt"], serialized_events)


if __name__ == "__main__":
    unittest.main()
