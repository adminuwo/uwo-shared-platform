import json
from pathlib import Path
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from services.ai_gateway.app import GatewayHandler
from services.ai_gateway.config import load_config
from services.ai_gateway.router import ModelRouter
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]


class GatewayHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        GatewayHandler.router = ModelRouter(load_config(ROOT / "infrastructure/config/ai-gateway.json"))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_health(self) -> None:
        with urlopen(f"{self.base_url}/healthz") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.load(response)["status"], "ok")

    def test_route(self) -> None:
        request = Request(
            f"{self.base_url}/v1/route",
            data=json.dumps({"tenant_id": "tenant-demo-in", "product": "aisa", "model": "uwo-general-v1", "region": "in"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            body = json.load(response)
            self.assertEqual(response.status, 200)
            self.assertEqual(body["provider"], "azure-openai-in")
            self.assertEqual(body["fallback"], ["aws-bedrock-in"])

    def test_disallowed_region_returns_forbidden(self) -> None:
        request = Request(
            f"{self.base_url}/v1/route",
            data=json.dumps({"tenant_id": "tenant-demo-in", "product": "aisa", "model": "uwo-general-v1", "region": "eu"}).encode(),
            method="POST",
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request)
        self.assertEqual(caught.exception.code, 403)
        self.assertEqual(json.load(caught.exception)["error"]["code"], "region_not_allowed")


if __name__ == "__main__":
    unittest.main()
