import unittest
from concurrent.futures import ThreadPoolExecutor

from packages.contracts import Product, VerifiedSubjectIdentity
from services.ai_gateway.audit import AuditEvent
from services.ai_gateway.authorization import EntitlementAuthorizer
from services.ai_gateway.billing import BillingCompensationError, ConfigBillingAuthorizer
from services.ai_gateway.config import GatewayConfig, Provider, TenantPolicy
from services.ai_gateway.content_safety import ContentSafetyError, ConfigContentSafetyAuthorizer
from services.ai_gateway.execution import SecureExecutionRequest, SecureExecutionService
from services.ai_gateway.providers import ProviderError, ProviderResponse, ProviderUsage
from services.ai_gateway.resilience import ResiliencePolicy, ResilientProviderExecutor
from services.ai_gateway.router import ModelRouter
from services.platform_billing.gateway import ServiceGatewayBilling
from services.platform_billing.errors import Conflict, RepositoryIntegrityError

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


class StaleOnceBillingService:
    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.capture_calls = 0

    def __getattr__(self, name):
        return getattr(self.wrapped, name)

    def capture_for_gateway(self, *args, **kwargs):
        self.capture_calls += 1
        if self.capture_calls == 1:
            raise Conflict("stale_version", "simulated concurrent write")
        return self.wrapped.capture_for_gateway(*args, **kwargs)


def config():
    provider = Provider("azure-openai-in", frozenset({"in"}), frozenset({"uwo-general-v1"}), 1, "azure-openai", "https://example.invalid", "env://KEY", {"uwo-general-v1": "deployment-a"})
    policy = TenantPolicy(frozenset({provider.id}), frozenset(), frozenset({"in"}), frozenset({Product.AISA}), frozenset({"uwo-general-v1"}), True, True, ("blocked-output",))
    return GatewayConfig((provider,), {TENANT: policy})


class BillingGatewayIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = make_billing_fixture(); provision(self.fixture); self.audit = CaptureAudit(); self.config = config()

    def service(self, adapter, lifecycle=None):
        lifecycle = lifecycle or ServiceGatewayBilling(self.fixture.service, EXECUTOR, 5_000)
        executor = ResilientProviderExecutor({adapter.provider_id: adapter}, ResiliencePolicy(max_attempts=1), sleep=lambda _: None)
        return SecureExecutionService(ModelRouter(self.config), EntitlementAuthorizer(self.config), ConfigBillingAuthorizer(self.config), ConfigContentSafetyAuthorizer(self.config), executor, self.audit, lifecycle)

    def request(self, request_id="req-gateway", prompt="safe"):
        return SecureExecutionRequest(request_id, TENANT, Product.AISA, "uwo-general-v1", "in", prompt)

    def test_insufficient_balance_denies_before_provider(self):
        adapter = Adapter(ProviderResponse("provider", "safe", "deployment-a", ProviderUsage(1_000, 0, 1_000)))
        with self.assertRaises(Exception) as caught:
            self.service(adapter).execute(IDENTITY, self.request())
        self.assertEqual(caught.exception.code, "insufficient_balance")
        self.assertEqual(adapter.calls, 0)

    def test_reservation_precedes_provider_and_success_captures_usage(self):
        fund(self.fixture)
        def before():
            balance = self.fixture.service.read_balance(PLATFORM, TENANT, "req-inspect")
            self.assertEqual(balance.reserved_microunits, 5_000)
        adapter = Adapter(ProviderResponse("provider-response", "safe", "deployment-a", ProviderUsage(1_000, 0, 1_000)), before)
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
        adapter = Adapter(ProviderResponse("provider", "blocked-output", "deployment-a", ProviderUsage(1_000, 0, 1_000)))
        with self.assertRaises(ContentSafetyError):
            self.service(adapter).execute(IDENTITY, self.request("req-output-denied"))
        balance = self.fixture.service.read_balance(PLATFORM, TENANT, "req-after-denial")
        self.assertEqual((balance.available_microunits, balance.reserved_microunits), (100_000, 0))
        self.assertEqual(len(self.fixture.usage._items), 0)

    def test_retry_does_not_duplicate_charge_usage_or_ledger(self):
        fund(self.fixture)
        adapter = Adapter(ProviderResponse("provider-retry", "safe", "deployment-a", ProviderUsage(1_000, 0, 1_000)))
        service = self.service(adapter)
        service.execute(IDENTITY, self.request("req-retry"))
        service.execute(IDENTITY, self.request("req-retry"))
        balance = self.fixture.service.read_balance(PLATFORM, TENANT, "req-after-retry")
        self.assertEqual(balance.available_microunits, 98_900)
        self.assertEqual(len(self.fixture.usage._items), 1)
        self.assertEqual(len(self.fixture.ledger._items), 4)

    def test_unrelated_ledger_grant_does_not_block_gateway_capture(self):
        fund(self.fixture)
        def unrelated_grant():
            balance = self.fixture.service.read_balance(PLATFORM, TENANT, "req-unrelated-read")
            self.fixture.service.grant_credits(PLATFORM, TENANT, 123, balance.version, "grant-unrelated-capture", "req-unrelated-grant")
        adapter = Adapter(ProviderResponse("provider-unrelated", "safe", "deployment-a", ProviderUsage(1_000, 0, 1_000)), unrelated_grant)
        self.service(adapter).execute(IDENTITY, self.request("req-unrelated-capture"))
        balance = self.fixture.service.read_balance(PLATFORM, TENANT, "req-unrelated-after")
        self.assertEqual((balance.available_microunits, balance.reserved_microunits), (99_023, 0))

    def test_unrelated_ledger_mutation_does_not_block_failure_release(self):
        fund(self.fixture)
        def unrelated_grant():
            balance = self.fixture.service.read_balance(PLATFORM, TENANT, "req-unrelated-fail-read")
            self.fixture.service.grant_credits(PLATFORM, TENANT, 77, balance.version, "grant-unrelated-release", "req-unrelated-release")
        adapter = Adapter(ProviderError("provider failed", fallback_allowed=False), unrelated_grant)
        with self.assertRaises(ProviderError):
            self.service(adapter).execute(IDENTITY, self.request("req-unrelated-release"))
        balance = self.fixture.service.read_balance(PLATFORM, TENANT, "req-unrelated-fail-after")
        self.assertEqual((balance.available_microunits, balance.reserved_microunits), (100_077, 0))

    def test_gateway_adapter_recreation_and_independent_captures_use_current_state(self):
        fund(self.fixture)
        first_adapter = ServiceGatewayBilling(self.fixture.service, EXECUTOR, 5_000)
        first = first_adapter.reserve(TENANT, Product.AISA, "uwo-general-v1", "req-recreate")
        recreated = ServiceGatewayBilling(self.fixture.service, EXECUTOR, 5_000)
        self.assertEqual(recreated.reserve(TENANT, Product.AISA, "uwo-general-v1", "req-recreate"), first)
        recreated.capture(first, "azure-openai-in", "deployment-a", "in", "provider-recreated", 1_000, 0, 1_000)
        ServiceGatewayBilling(self.fixture.service, EXECUTOR, 5_000).capture(first, "azure-openai-in", "deployment-a", "in", "provider-recreated", 1_000, 0, 1_000)

        receipts = [recreated.reserve(TENANT, Product.AISA, "uwo-general-v1", f"req-independent-{index}") for index in (1, 2)]
        def capture(receipt):
            recreated.capture(receipt, "azure-openai-in", "deployment-a", "in", f"provider-{receipt.request_id}", 1_000, 0, 1_000)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(capture, receipts))
        balance = self.fixture.service.read_balance(PLATFORM, TENANT, "req-independent-after")
        self.assertEqual(balance.reserved_microunits, 0)
        self.assertEqual(len(self.fixture.usage._items), 3)

    def test_gateway_reservation_failure_rolls_back_without_orphan(self):
        fund(self.fixture)
        lifecycle = ServiceGatewayBilling(self.fixture.service, EXECUTOR, 5_000)
        self.fixture.failures.fail_next("ledger_write")
        with self.assertRaises(RepositoryIntegrityError):
            lifecycle.reserve(TENANT, Product.AISA, "uwo-general-v1", "req-orphan-check")
        self.assertEqual(self.fixture.reservations._items, {})
        balance = self.fixture.service.read_balance(PLATFORM, TENANT, "req-orphan-check-after")
        self.assertEqual((balance.available_microunits, balance.reserved_microunits), (100_000, 0))

    def test_missing_usage_releases_reservation_and_records_no_usage(self):
        fund(self.fixture)
        adapter = Adapter(ProviderResponse("provider-missing-usage", "must not return", "deployment-a"))
        with self.assertRaises(ProviderError) as caught:
            self.service(adapter).execute(IDENTITY, self.request("req-missing-usage"))
        self.assertEqual(caught.exception.code, "missing_usage")
        balance = self.fixture.service.read_balance(PLATFORM, TENANT, "req-missing-usage-after")
        self.assertEqual((balance.available_microunits, balance.reserved_microunits), (100_000, 0))
        self.assertEqual(len(self.fixture.usage._items), 0)

    def test_capture_failure_is_retryable_without_second_provider_call(self):
        fund(self.fixture)
        def fail_capture_write():
            self.fixture.failures.fail_next("ledger_write")
        adapter = Adapter(ProviderResponse("provider-capture-retry", "safe", "deployment-a", ProviderUsage(1_000, 0, 1_000)), fail_capture_write)
        service = self.service(adapter)
        with self.assertRaises(RepositoryIntegrityError):
            service.execute(IDENTITY, self.request("req-capture-recovery"))
        self.assertEqual(adapter.calls, 1)
        result = service.execute(IDENTITY, self.request("req-capture-recovery"))
        self.assertEqual(result.provider_request_id, "provider-capture-retry")
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(len(self.fixture.usage._items), 1)
        self.assertEqual(self.fixture.service.read_balance(PLATFORM, TENANT, "req-capture-recovery-after").reserved_microunits, 0)

    def test_caller_retry_after_stale_conflict_reuses_provider_result(self):
        fund(self.fixture)
        wrapped = StaleOnceBillingService(self.fixture.service)
        lifecycle = ServiceGatewayBilling(wrapped, EXECUTOR, 5_000)
        adapter = Adapter(ProviderResponse("provider-stale-retry", "safe", "deployment-a", ProviderUsage(1_000, 0, 1_000)))
        service = self.service(adapter, lifecycle)
        with self.assertRaises(Conflict) as caught:
            service.execute(IDENTITY, self.request("req-stale-retry"))
        self.assertEqual(caught.exception.code, "stale_version")
        service.execute(IDENTITY, self.request("req-stale-retry"))
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(wrapped.capture_calls, 2)
        self.assertEqual(self.fixture.service.read_balance(PLATFORM, TENANT, "req-stale-retry-after").reserved_microunits, 0)

    def test_provider_and_output_failures_report_compensation_failure_without_hiding_cause(self):
        fund(self.fixture)
        def fail_release():
            self.fixture.failures.fail_next("ledger_write")
        provider_failure = Adapter(ProviderError("provider secret detail", fallback_allowed=False), fail_release)
        with self.assertRaises(BillingCompensationError) as provider_caught:
            self.service(provider_failure).execute(IDENTITY, self.request("req-provider-compensation"))
        self.assertIsInstance(provider_caught.exception.original_failure, ProviderError)
        self.assertEqual(self.fixture.service.read_balance(PLATFORM, TENANT, "req-provider-compensation-after").reserved_microunits, 5_000)

        second = make_billing_fixture(); provision(second); fund(second)
        self.fixture = second
        output_failure = Adapter(ProviderResponse("provider-output-compensation", "blocked-output", "deployment-a", ProviderUsage(1, 0, 1)), fail_release)
        with self.assertRaises(BillingCompensationError) as output_caught:
            self.service(output_failure).execute(IDENTITY, self.request("req-output-compensation"))
        self.assertIsInstance(output_caught.exception.original_failure, ContentSafetyError)
        compensation_events = [event for event in self.audit.events if event.event_type == "billing-compensation-failed"]
        self.assertEqual(len(compensation_events), 2)


if __name__ == "__main__": unittest.main()
