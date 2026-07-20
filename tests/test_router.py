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
        self.assertEqual(first.fallback, ("openai-in",))

    def test_block_policy_excludes_provider(self) -> None:
        result = self.router.route(self.request(tenant="tenant-block-example"))
        self.assertEqual(result.provider, "openai-in")
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
    def altered_config(self):
        import json

        return json.loads((ROOT / "infrastructure/config/ai-gateway.json").read_text())

    def write_and_load(self, config):
        import json
        import tempfile

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.json"
        path.write_text(json.dumps(config))
        return load_config(path)

    def test_duplicate_provider_priority_is_rejected(self) -> None:
        config = self.altered_config()
        config["providers"][1]["priority"] = config["providers"][0]["priority"]
        with self.assertRaises(ConfigurationError):
            self.write_and_load(config)

    def test_duplicate_provider_id_is_rejected(self) -> None:
        config = self.altered_config()
        config["providers"][1]["id"] = config["providers"][0]["id"]
        with self.assertRaises(ConfigurationError):
            self.write_and_load(config)

    def test_unknown_product_entitlement_is_rejected(self) -> None:
        config = self.altered_config()
        config["tenant_policies"]["tenant-demo-in"]["allowed_products"] = ["not-a-product"]
        with self.assertRaises(ConfigurationError):
            self.write_and_load(config)

    def test_provider_model_maps_include_general_and_legal_aliases(self) -> None:
        config = self.altered_config()
        self.assertNotIn("api_version", config["providers"][0])
        loaded = self.write_and_load(config)
        azure = loaded.providers[0]
        self.assertEqual(azure.model_map["uwo-general-v1"], "azure-in-general-deployment")
        self.assertEqual(azure.model_map["uwo-legal-v1"], "azure-in-legal-deployment")

    def test_missing_model_mapping_is_rejected(self) -> None:
        config = self.altered_config()
        del config["providers"][0]["model_map"]["uwo-legal-v1"]
        with self.assertRaises(ConfigurationError):
            self.write_and_load(config)

    def test_undeclared_extra_model_mapping_is_rejected(self) -> None:
        config = self.altered_config()
        config["providers"][0]["model_map"]["undeclared-alias"] = "provider-model"
        with self.assertRaises(ConfigurationError):
            self.write_and_load(config)

    def test_empty_provider_model_mapping_is_rejected(self) -> None:
        config = self.altered_config()
        config["providers"][0]["model_map"]["uwo-general-v1"] = ""
        with self.assertRaises(ConfigurationError):
            self.write_and_load(config)

    def test_invalid_content_safety_policy_is_rejected(self) -> None:
        config = self.altered_config()
        config["tenant_policies"]["tenant-demo-in"]["content_safety"]["blocked_terms"] = "not-a-list"
        with self.assertRaises(ConfigurationError):
            self.write_and_load(config)


if __name__ == "__main__":
    unittest.main()
