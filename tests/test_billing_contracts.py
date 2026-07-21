import unittest

from packages.contracts import (
    BillingAccount, BillingAccountStatus, CreditReservation, LedgerEntry, LedgerEntryType,
    Product, ReservationStatus, UsageDimensions,
)
from services.platform_billing.pricing import calculate_charge, round_up_ratio

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


if __name__ == "__main__":
    unittest.main()
