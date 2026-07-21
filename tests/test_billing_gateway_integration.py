import unittest

from packages.contracts import Product, VerifiedSubjectIdentity
from services.ai_gateway.audit import AuditEvent
from services.ai_gateway.authorization import EntitlementAuthorizer
from services.ai_gateway.billing import ConfigBillingAuthorizer
from services.ai_gateway.config import GatewayConfig, Provider, TenantPolicy
from services.ai_gateway.content_safety import ContentSafetyError, ConfigContentSafetyAuthorizer
from services.ai_gateway.execution import SecureExecutionRequest, SecureExecutionService
from services.ai_gateway.providers import ProviderError, ProviderResponse
from services.ai_gateway.resilience import ResiliencePolicy, ResilientProviderExecutor
from services.ai_gateway.router import ModelRouter
from services.platform_billing.gateway import ServiceGatewayBilling

from billing_support import EXECUTOR, fund, make_billing_fixture, provision
from control_plane_support import PLATFORM


TENANT = "tenant-billing"
IDENTITY = VerifiedSubjectIdentity("gateway-user", TENANT, "2026-07-20T12:00:00+00:00")


class CaptureAudit:
    def __init__(self): self.events = []
    def emit(self, event: AuditEvent): self.events.append(event)


class Adapter:
    provider_id = "azure-openai-in"
    def __init__(self, outcome, before=None):
        self.outcome = outcome; self.before = before; self.calls = 0
    def execute(self, request, timeout_seconds):
        self.calls += 1
        if self.before: self.before()
        if isinstance(self.outcome, Exception): raise self.outcome
        return self.outcome


def config():
    provider = Provider("azure-openai-in", frozenset({"in"}), frozenset({"uwo-general-v1"}), 1, "azure-openai", "https://example.invalid", "env://KEY", {"uwo-general-v1": "deployment-a"})
    policy = TenantPolicy(frozenset({provider.id}), frozenset(), frozenset({"in"}), frozenset({Product.AISA}), frozenset({"uwo-general-v1"}), True, True, ("blocked-output",))
    return GatewayConfig((provider,), {TENANT: policy})


class BillingGatewayIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = make_billing_fixture(); provision(self.fixture); self.audit = CaptureAudit(); self.config = config()

    def service(self, adapter):
        lifecycle = ServiceGatewayBilling(self.fixture.service, EXECUTOR, 5_000)
        executor = ResilientProviderExecutor({adapter.provider_id: adapter}, ResiliencePolicy(max_attempts=1), sleep=lambda _: None)
        return SecureExecutionService(ModelRouter(self.config), EntitlementAuthorizer(self.config), ConfigBillingAuthorizer(self.config), ConfigContentSafetyAuthorizer(self.config), executor, self.audit, lifecycle)

    def request(self, request_id="req-gateway", prompt="safe"):
        return SecureExecutionRequest(request_id, TENANT, Product.AISA, "uwo-general-v1", "in", prompt)

    def test_insufficient_balance_denies_before_provider(self):
        adapter = Adapter(ProviderResponse("provider", "safe", "deployment-a", 1_000, 0, 1_000))
        with self.assertRaises(Exception) as caught:
            self.service(adapter).execute(IDENTITY, self.request())
        self.assertEqual(caught.exception.code, "insufficient_balance")
        self.assertEqual(adapter.calls, 0)

    def test_reservation_precedes_provider_and_success_captures_usage(self):
        fund(self.fixture)
        def before():
            balance = self.fixture.service.read_balance(PLATFORM, TENANT, "req-inspect")
            self.assertEqual(balance.reserved_microunits, 5_000)
        adapter = Adapter(ProviderResponse("provider-response", "safe", "deployment-a", 1_000, 0, 1_000), before)
        result = self.service(adapter).execute(IDENTITY, self.request())
        self.assertEqual(result.provider_request_id, "provider-response")
        balance = self.fixture.service.read_balance(PLATFORM, TENANT, "req-after")
        self.assertEqual(balance.reserved_microunits, 0)
        self.assertEqual(balance.available_microunits, 98_900)
        self.assertEqual(len(self.fixture.usage._items), 1)

    def test_provider_failure_releases_reservation(self):
        fund(self.fixture)
        adapter = Adapter(ProviderError("provider failed"))
        with self.assertRaises(Exception):
            self.service(adapter).execute(IDENTITY, self.request("req-failure"))
        balance = self.fixture.service.read_balance(PLATFORM, TENANT, "req-after-failure")
        self.assertEqual((balance.available_microunits, balance.reserved_microunits), (100_000, 0))

    def test_output_safety_denial_releases_without_charge(self):
        fund(self.fixture)
        adapter = Adapter(ProviderResponse("provider", "blocked-output", "deployment-a", 1_000, 0, 1_000))
        with self.assertRaises(ContentSafetyError):
            self.service(adapter).execute(IDENTITY, self.request("req-output-denied"))
        balance = self.fixture.service.read_balance(PLATFORM, TENANT, "req-after-denial")
        self.assertEqual((balance.available_microunits, balance.reserved_microunits), (100_000, 0))
        self.assertEqual(len(self.fixture.usage._items), 0)

    def test_retry_does_not_duplicate_charge_usage_or_ledger(self):
        fund(self.fixture)
        adapter = Adapter(ProviderResponse("provider-retry", "safe", "deployment-a", 1_000, 0, 1_000))
        service = self.service(adapter)
        service.execute(IDENTITY, self.request("req-retry"))
        service.execute(IDENTITY, self.request("req-retry"))
        balance = self.fixture.service.read_balance(PLATFORM, TENANT, "req-after-retry")
        self.assertEqual(balance.available_microunits, 98_900)
        self.assertEqual(len(self.fixture.usage._items), 1)
        self.assertEqual(len(self.fixture.ledger._items), 4)


if __name__ == "__main__": unittest.main()
