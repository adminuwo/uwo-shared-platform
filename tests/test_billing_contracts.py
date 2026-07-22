import unittest

from packages.contracts import (
    BillingAccount, BillingAccountStatus, CreditReservation, LedgerEntry, LedgerEntryType,
    Product, ReservationStatus, UsageDimensions,
)
from services.platform_billing.pricing import calculate_charge, round_up_ratio
from services.platform_billing.errors import Conflict, RepositoryIntegrityError
from services.platform_billing.in_memory import FailureInjector, InMemoryBillingAccountRepository, InMemoryLedgerRepository, InMemoryRateCardRepository

from billing_support import FUTURE, NOW, example_rate_card


class BillingContractTests(unittest.TestCase):
    def test_integer_contracts_and_versions(self) -> None:
        account = BillingAccount("billing:tenant", "tenant", BillingAccountStatus.ACTIVE, NOW, NOW, 1)
        self.assertEqual(account.version, 1)
        with self.assertRaises(ValueError):
            BillingAccount("billing:tenant", "tenant", BillingAccountStatus.ACTIVE, NOW, NOW, 0)
        with self.assertRaises(ValueError):
            LedgerEntry("entry", "billing:tenant", "tenant", LedgerEntryType.CREDIT_GRANT, 1.5, 1, 0, "ref", NOW, "actor", 2)
        with self.assertRaises(ValueError):
            LedgerEntry("entry", "billing:tenant", "tenant", LedgerEntryType.CREDIT_GRANT, 1, True, 0, "ref", NOW, "actor", 2)

    def test_every_ledger_type_has_exact_canonical_deltas(self) -> None:
        valid = {
            LedgerEntryType.CREDIT_GRANT: (10, 0),
            LedgerEntryType.CREDIT_ADJUSTMENT: (-10, 0),
            LedgerEntryType.USAGE_RESERVATION: (-10, 10),
            LedgerEntryType.USAGE_CAPTURE: (0, -10),
            LedgerEntryType.RESERVATION_RELEASE: (10, -10),
            LedgerEntryType.REFUND: (10, 0),
        }
        for index, (entry_type, deltas) in enumerate(valid.items()):
            entry = LedgerEntry(f"entry-{index}", "billing:tenant", "tenant", entry_type, 10, *deltas, f"ref-{index}", NOW, "actor", index + 2)
            self.assertEqual((entry.available_delta_microunits, entry.reserved_delta_microunits), deltas)
        invalid = (
            (LedgerEntryType.CREDIT_GRANT, -10, 0),
            (LedgerEntryType.CREDIT_ADJUSTMENT, 0, 0),
            (LedgerEntryType.CREDIT_ADJUSTMENT, 10, 1),
            (LedgerEntryType.USAGE_RESERVATION, -9, 10),
            (LedgerEntryType.USAGE_CAPTURE, 1, -10),
            (LedgerEntryType.RESERVATION_RELEASE, 10, 0),
            (LedgerEntryType.REFUND, 0, 10),
        )
        for index, (entry_type, available, reserved) in enumerate(invalid):
            with self.subTest(entry_type=entry_type, index=index), self.assertRaises(ValueError):
                LedgerEntry(f"bad-{index}", "billing:tenant", "tenant", entry_type, 10, available, reserved, f"bad-ref-{index}", NOW, "actor", 2)

    def test_repository_rejects_account_mismatch_duplicate_and_nonsequential_entries(self) -> None:
        failures = FailureInjector()
        accounts = InMemoryBillingAccountRepository(failures)
        account = BillingAccount("billing:tenant", "tenant", BillingAccountStatus.ACTIVE, NOW, NOW, 1)
        accounts.create(account)
        ledger = InMemoryLedgerRepository(accounts, failures)
        grant = LedgerEntry("grant", account.account_id, account.tenant_id, LedgerEntryType.CREDIT_GRANT, 10, 10, 0, "grant-ref", NOW, "actor", 2)
        self.assertEqual(ledger.append(grant, 1).available_microunits, 10)
        with self.assertRaises(Conflict):
            ledger.append(grant, 2)
        bad_version = LedgerEntry("bad-version", account.account_id, account.tenant_id, LedgerEntryType.REFUND, 1, 1, 0, "refund-ref", NOW, "actor", 4)
        with self.assertRaises(Conflict):
            ledger.append(bad_version, 2)
        mismatch = LedgerEntry("mismatch", account.account_id, "other-tenant", LedgerEntryType.REFUND, 1, 1, 0, "mismatch-ref", NOW, "actor", 3)
        with self.assertRaises(RepositoryIntegrityError):
            ledger.append(mismatch, 2)

    def test_reservation_integrity(self) -> None:
        with self.assertRaises(ValueError):
            CreditReservation("reservation", "billing:tenant", "tenant", Product.AISA, "uwo-general-v1", "request", 100, 80, 30, ReservationStatus.PARTIALLY_CAPTURED, NOW, FUTURE, NOW, 1)

    def test_deterministic_rounding_is_integer_ceiling(self) -> None:
        self.assertEqual(round_up_ratio(1, 1), 1)
        self.assertEqual(round_up_ratio(1_001, 1_000), 1_001)
        calculation = calculate_charge(example_rate_card(), Product.AISA, "uwo-general-v1", "azure-openai-in", "in", UsageDimensions(1, 1, 2), NOW)
        self.assertEqual(calculation.total_charge_microunits, 103)

    def test_unknown_rate_and_negative_usage_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            UsageDimensions(-1, 0, -1)
        with self.assertRaises(Exception) as caught:
            calculate_charge(example_rate_card(), Product.AISA, "unknown-model", "azure-openai-in", "in", UsageDimensions(1, 0, 1), NOW)
        self.assertEqual(caught.exception.code, "unknown_rate")

    def test_rate_card_activation_is_time_aware_and_deterministic(self) -> None:
        later = "2026-07-20T12:30:00+00:00"
        repository = InMemoryRateCardRepository((example_rate_card(1, NOW), example_rate_card(2, later, 2)))
        self.assertEqual(repository.active_at(NOW).version, 1)
        self.assertEqual(repository.active_at(later).version, 2)
        self.assertEqual(repository.active_at(FUTURE).version, 2)
        with self.assertRaises(Conflict) as caught:
            repository.add(example_rate_card(3, later, 3))
        self.assertEqual(caught.exception.code, "ambiguous_rate_card")
        future_only = InMemoryRateCardRepository((example_rate_card(5, FUTURE),))
        with self.assertRaises(Exception) as missing:
            future_only.active_at(NOW)
        self.assertEqual(missing.exception.code, "unknown_rate_card")


if __name__ == "__main__":
    unittest.main()
