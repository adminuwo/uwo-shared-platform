import unittest

from services.ai_gateway.auth import AuthenticationError, HmacBearerAuthenticator, issue_test_token

KEY = "test-signing-key-with-at-least-32-chars"


class AuthenticationTests(unittest.TestCase):
    def test_valid_token_returns_verified_identity(self) -> None:
        token = issue_test_token(KEY, "subject-1", "tenant-1", 101)
        identity = HmacBearerAuthenticator(KEY, clock=lambda: 100).authenticate(f"Bearer {token}")
        self.assertEqual(identity.subject, "subject-1")
        self.assertEqual(identity.tenant_id, "tenant-1")

    def test_tampered_token_is_rejected(self) -> None:
        token = issue_test_token(KEY, "subject-1", "tenant-1", 101)
        with self.assertRaises(AuthenticationError) as caught:
            HmacBearerAuthenticator(KEY, clock=lambda: 100).authenticate(f"Bearer {token}x")
        self.assertEqual(caught.exception.code, "invalid_token")

    def test_expired_token_is_rejected(self) -> None:
        token = issue_test_token(KEY, "subject-1", "tenant-1", 100)
        with self.assertRaises(AuthenticationError) as caught:
            HmacBearerAuthenticator(KEY, clock=lambda: 100).authenticate(f"Bearer {token}")
        self.assertEqual(caught.exception.code, "expired_token")

    def test_wrong_audience_is_rejected(self) -> None:
        token = issue_test_token(KEY, "subject-1", "tenant-1", 101, audience="another-service")
        with self.assertRaises(AuthenticationError) as caught:
            HmacBearerAuthenticator(KEY, clock=lambda: 100).authenticate(f"Bearer {token}")
        self.assertEqual(caught.exception.code, "invalid_token")


if __name__ == "__main__":
    unittest.main()
