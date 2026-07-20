import unittest
import json
from pathlib import Path
import tempfile

from tooling.validate_security import validate


class SecurityValidationTests(unittest.TestCase):
    def test_repository_security_configuration_is_valid(self) -> None:
        self.assertEqual(validate(), [])

    def test_inline_credentials_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "infrastructure/config").mkdir(parents=True)
            config = {"providers": [{"adapter": "openai", "endpoint": "https://example.invalid", "secret_ref": "env://KEY", "api_key": "forbidden"}], "tenant_policies": {"tenant": {"content_safety": {"enabled": True}}}}
            (root / "infrastructure/config/ai-gateway.json").write_text(json.dumps(config))
            (root / ".gitignore").write_text(".env\n.env.*\n")
            errors = validate(root)
        self.assertTrue(any("credential fields" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
