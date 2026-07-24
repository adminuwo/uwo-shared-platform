from __future__ import annotations

from dataclasses import dataclass

from packages.contracts import ModelPrice, Product, RateCard, VerifiedSubjectIdentity
from services.platform_billing.audit import BillingAuditEvent
from services.platform_billing.authorization import BillingAuthorizer
from services.platform_billing.in_memory import (
    FailureInjector,
    InMemoryBillingAccountRepository,
    InMemoryBillingUnitOfWorkFactory,
    InMemoryIdempotencyRepository,
    InMemoryLedgerRepository,
    InMemoryRateCardRepository,
    InMemoryReservationRepository,
    InMemoryUsageEventRepository,
)
from services.platform_billing.service import PlatformBillingService

from control_plane_support import PLATFORM, bootstrap_tenant_admin, make_fixture

NOW = "2026-07-20T12:00:00+00:00"
FUTURE = "2026-07-20T13:00:00+00:00"
PAST = "2026-07-20T11:00:00+00:00"
EXECUTOR = VerifiedSubjectIdentity("billing-executor", "platform", NOW)


class CaptureBillingAudit:
    def __init__(self) -> None:
        self.events: list[BillingAuditEvent] = []

    def emit(self, event: BillingAuditEvent) -> None:
        self.events.append(event)


def example_rate_card(version: int = 1, effective_at: str = NOW, multiplier: int = 1) -> RateCard:
    prices = []
    for product in Product:
        for model in ("uwo-general-v1", "uwo-legal-v1"):
            for provider in ("azure-openai-in", "openai-in"):
                prices.append(ModelPrice(product, model, provider, "in", 1_000 * multiplier, 2_000 * multiplier, 100 * multiplier))
    return RateCard("example-test-rate-card", version, effective_at, tuple(sorted(prices, key=lambda item: item.key)), effective_at)


@dataclass
class BillingFixture:
    service: PlatformBillingService
    control: object
    accounts: InMemoryBillingAccountRepository
    ledger: InMemoryLedgerRepository
    reservations: InMemoryReservationRepository
    usage: InMemoryUsageEventRepository
    rate_cards: InMemoryRateCardRepository
    idempotency: InMemoryIdempotencyRepository
    failures: FailureInjector
    audit: CaptureBillingAudit


def make_billing_fixture(*, clock=lambda: NOW, rate_card_values: tuple[RateCard, ...] | None = None, event_recorder=None) -> BillingFixture:
    control = make_fixture()
    control.subjects.provision(EXECUTOR.subject)
    failures = FailureInjector()
    accounts = InMemoryBillingAccountRepository(failures)
    ledger = InMemoryLedgerRepository(accounts, failures)
    reservations = InMemoryReservationRepository(failures)
    usage = InMemoryUsageEventRepository(failures)
    rate_cards = InMemoryRateCardRepository(rate_card_values or (example_rate_card(),))
    idempotency = InMemoryIdempotencyRepository(failures)
    audit = CaptureBillingAudit()
    authorizer = BillingAuthorizer(control.tenants, control.subjects, control.service._authorizer, frozenset({EXECUTOR.subject}))
    unit_of_work = InMemoryBillingUnitOfWorkFactory(accounts, ledger, reservations, usage, rate_cards, idempotency)
    service = PlatformBillingService(control.tenants, accounts, ledger, reservations, usage, rate_cards, unit_of_work, authorizer, audit, clock=clock, event_recorder=event_recorder)
    return BillingFixture(service, control, accounts, ledger, reservations, usage, rate_cards, idempotency, failures, audit)


def provision(fixture: BillingFixture, tenant_id: str = "tenant-billing") -> None:
    fixture.control.service.create_tenant(PLATFORM, tenant_id, "Billing Tenant", "in", f"create-{tenant_id}", f"req-create-{tenant_id}")
    fixture.service.create_account(PLATFORM, tenant_id, f"account-{tenant_id}", f"req-account-{tenant_id}")


def fund(fixture: BillingFixture, tenant_id: str = "tenant-billing", amount: int = 100_000):
    balance = fixture.service.read_balance(PLATFORM, tenant_id, "req-balance")
    return fixture.service.grant_credits(PLATFORM, tenant_id, amount, balance.version, f"grant-{tenant_id}-{amount}", "req-grant")
