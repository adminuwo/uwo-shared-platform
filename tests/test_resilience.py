import unittest

from services.ai_gateway.providers import ProviderError, ProviderRequest, ProviderResponse, ProviderTimeout
from services.ai_gateway.resilience import ProviderUnavailable, ResiliencePolicy, ResilientProviderExecutor


class SequenceAdapter:
    def __init__(self, provider_id, outcomes):
        self.provider_id = provider_id
        self.outcomes = list(outcomes)
        self.calls = 0

    def execute(self, request, timeout_seconds):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


REQUEST = ProviderRequest("req", "tenant", "model", "prompt")
RESPONSE = ProviderResponse("provider-req", "result")


class ResilienceTests(unittest.TestCase):
    def policy(self, threshold=3):
        return ResiliencePolicy(timeout_seconds=2, max_attempts=2, retry_backoff_seconds=0, circuit_failure_threshold=threshold, circuit_reset_seconds=30)

    def test_retries_transient_failure(self) -> None:
        adapter = SequenceAdapter("primary", [ProviderTimeout(), RESPONSE])
        result = ResilientProviderExecutor({"primary": adapter}, self.policy(), sleep=lambda _: None).execute(("primary",), REQUEST)
        self.assertEqual(result.provider_id, "primary")
        self.assertEqual(adapter.calls, 2)

    def test_non_retryable_failure_uses_fallback(self) -> None:
        primary = SequenceAdapter("primary", [ProviderError("bad request")])
        fallback = SequenceAdapter("fallback", [RESPONSE])
        result = ResilientProviderExecutor({"primary": primary, "fallback": fallback}, self.policy(), sleep=lambda _: None).execute(("primary", "fallback"), REQUEST)
        self.assertEqual(result.provider_id, "fallback")
        self.assertEqual(primary.calls, 1)

    def test_open_circuit_skips_provider(self) -> None:
        primary = SequenceAdapter("primary", [ProviderTimeout(), ProviderTimeout()])
        executor = ResilientProviderExecutor({"primary": primary}, self.policy(threshold=1), clock=lambda: 10, sleep=lambda _: None)
        with self.assertRaises(ProviderUnavailable):
            executor.execute(("primary",), REQUEST)
        with self.assertRaises(ProviderUnavailable) as caught:
            executor.execute(("primary",), REQUEST)
        self.assertIn("primary:circuit_open", caught.exception.failures)
        self.assertEqual(primary.calls, 1)


if __name__ == "__main__":
    unittest.main()
