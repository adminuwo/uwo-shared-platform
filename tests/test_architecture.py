import json
from pathlib import Path
import unittest

from tooling.validate_architecture import validate


class ArchitectureTests(unittest.TestCase):
    def test_manifest_is_valid(self) -> None:
        self.assertEqual(validate(), [])

    def test_control_plane_is_registered_with_versioned_endpoints(self) -> None:
        manifest = json.loads((Path(__file__).resolve().parents[1] / "architecture/manifest.json").read_text())
        component = next(item for item in manifest["components"] if item["id"] == "platform-control-plane")
        self.assertEqual(component["path"], "services/platform_control_plane")
        self.assertTrue(all(endpoint == "GET /healthz" or "/v1/" in endpoint for endpoint in component["endpoints"]))

    def test_billing_service_is_registered_with_versioned_endpoints(self) -> None:
        manifest = json.loads((Path(__file__).resolve().parents[1] / "architecture/manifest.json").read_text())
        component = next(item for item in manifest["components"] if item["id"] == "platform-billing")
        self.assertEqual(component["path"], "services/platform_billing")
        self.assertIn("billing-and-credits", component["capabilities"])
        self.assertTrue(all(endpoint == "GET /healthz" or "/v1/" in endpoint for endpoint in component["endpoints"]))

    def test_phase3c_services_are_registered_with_versioned_endpoints(self) -> None:
        manifest = json.loads((Path(__file__).resolve().parents[1] / "architecture/manifest.json").read_text())
        expected = {"platform-storage", "platform-notifications", "platform-analytics", "platform-audit"}
        components = {item["id"]: item for item in manifest["components"] if item["id"] in expected}
        self.assertEqual(set(components), expected)
        for component in components.values():
            self.assertTrue(all(endpoint == "GET /healthz" or "/v1/" in endpoint for endpoint in component["endpoints"]))


if __name__ == "__main__":
    unittest.main()
