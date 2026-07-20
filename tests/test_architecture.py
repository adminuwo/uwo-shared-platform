import unittest

from tooling.validate_architecture import validate


class ArchitectureTests(unittest.TestCase):
    def test_manifest_is_valid(self) -> None:
        self.assertEqual(validate(), [])


if __name__ == "__main__":
    unittest.main()
