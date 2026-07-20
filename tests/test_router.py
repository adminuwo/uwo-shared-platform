from pathlib import Path
import unittest

from packages.contracts import Product
from services.ai_gateway.config import ConfigurationError, load_config
from services.ai_gateway.router import ModelRouter, RouteRequest, RoutingError

ROOT = Path(__file__).resolve().parents[1]


class ModelRouterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.router = ModelRouter(load_config(ROOT / "infrastructure/config/ai-gateway.json"))

    def request(self, tenant: str = "tenant-demo-in", region: str = "in", model: str = "uwo-general-v1") -> RouteRequest:
        return RouteRequest(tenant, Product.AISA, model, region)

    def test_selection_and_fallback_are_deterministic(self) -> None:
        first = self.router.route(self.request())
        second = self.router.route(self.request())
        self.assertEqual(first, second)
        self.assertEqual(first.provider, "azure-openai-in")
        self.assertEqual(first.fallback, ("aws-bedrock-in",))

    def test_block_policy_excludes_provider(self) -> None:
        result = self.router.route(self.request(tenant="tenant-block-example"))
        self.assertEqual(result.provider, "aws-bedrock-in")
        self.assertEqual(result.fallback, ())

    def test_region_policy_is_enforced_before_provider_selection(self) -> None:
        with self.assertRaisesRegex(RoutingError, "region") as caught:
            self.router.route(self.request(region="eu"))
        self.assertEqual(caught.exception.code, "region_not_allowed")

    def test_unknown_tenant_is_denied_by_default(self) -> None:
        with self.assertRaises(RoutingError) as caught:
            self.router.route(self.request(tenant="unknown"))
        self.assertEqual(caught.exception.code, "unknown_tenant")

    def test_unsupported_model_has_no_eligible_provider(self) -> None:
        with self.assertRaises(RoutingError) as caught:
            self.router.route(self.request(model="unapproved-model"))
        self.assertEqual(caught.exception.code, "no_eligible_provider")


class ConfigurationTests(unittest.TestCase):
    def test_duplicate_provider_priority_is_rejected(self) -> None:
        import json
        import tempfile

        config = json.loads((ROOT / "infrastructure/config/ai-gateway.json").read_text())
        config["providers"][1]["priority"] = config["providers"][0]["priority"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config))
            with self.assertRaises(ConfigurationError):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
