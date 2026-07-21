from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from packages.contracts import BillingAccountStatus, MembershipStatus, Product, ReservationStatus, TenantStatus, UsageDimensions
from services.platform_billing.errors import Conflict, PaymentRequired, RepositoryIntegrityError

from billing_support import EXECUTOR, FUTURE, NOW, PAST, example_rate_card, fund, make_billing_fixture, provision
from control_plane_support import ADMIN_A, PLATFORM


class PlatformBillingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = make_billing_fixture()
        provision(self.fixture)

    def test_account_creation_duplicate_and_idempotent_replay(self) -> None:
        replay = self.fixture.service.create_account(PLATFORM, "tenant-billing", "account-tenant-billing", "req-replay")
        self.assertFalse(replay.created)
        self.assertEqual(replay.account.version, 1)
        with self.assertRaises(Conflict):
            self.fixture.service.create_account(PLATFORM, "tenant-billing", "different-key", "req-duplicate")

    def test_account_replay_returns_original_snapshot_after_status_change(self) -> None:
        self.fixture.service.set_account_status(PLATFORM, "tenant-billing", BillingAccountStatus.SUSPENDED, 1, "req-suspend")
        replay = self.fixture.service.create_account(PLATFORM, "tenant-billing", "account-tenant-billing", "req-account-replay")
        self.assertFalse(replay.created)
        self.assertEqual((replay.account.status, replay.account.version), (BillingAccountStatus.ACTIVE, 1))

    def test_account_status_transitions_and_closed_account(self) -> None:
        suspended = self.fixture.service.set_account_status(PLATFORM, "tenant-billing", BillingAccountStatus.SUSPENDED, 1, "req-suspend")
        active = self.fixture.service.set_account_status(PLATFORM, "tenant-billing", BillingAccountStatus.ACTIVE, suspended.version, "req-active")
        closed = self.fixture.service.set_account_status(PLATFORM, "tenant-billing", BillingAccountStatus.CLOSED, active.version, "req-close")
        with self.assertRaises(Conflict):
            self.fixture.service.set_account_status(PLATFORM, "tenant-billing", BillingAccountStatus.ACTIVE, closed.version, "req-reopen")

    def test_credit_grant_adjustment_refund_and_negative_balance_prevention(self) -> None:
        granted = fund(self.fixture, amount=10_000)
        adjusted = self.fixture.service.adjust_credits(PLATFORM, "tenant-billing", -2_000, granted.balance.version, "adjust", "req-adjust")
        refunded = self.fixture.service.refund(PLATFORM, "tenant-billing", 500, adjusted.balance.version, "refund", "req-refund")
        self.assertEqual(refunded.balance.available_microunits, 8_500)
        with self.assertRaises(Conflict):
            self.fixture.service.adjust_credits(PLATFORM, "tenant-billing", -9_000, refunded.balance.version, "overdraw", "req-overdraw")

    def test_insufficient_balance_denies_authorization_and_reservation(self) -> None:
        with self.assertRaises(PaymentRequired):
            self.fixture.service.authorize_estimated_charge(EXECUTOR, "tenant-billing", 1, "req-denied")
        with self.assertRaises(PaymentRequired):
            self.fixture.service.reserve(EXECUTOR, "tenant-billing", Product.AISA, "uwo-general-v1", "req-reserve-denied", 1, FUTURE, 1, "reserve-denied")

    def _reserve(self, amount=5_000, request_id="req-model"):
        funded = fund(self.fixture)
        return self.fixture.service.reserve(EXECUTOR, "tenant-billing", Product.AISA, "uwo-general-v1", request_id, amount, FUTURE, funded.balance.version, f"reserve-{request_id}")

    def test_reservation_partial_capture_full_capture_and_release(self) -> None:
        reserved = self._reserve()
        partial = self.fixture.service.capture(EXECUTOR, reserved.reservation.reservation_id, "usage-part", "azure-openai-in", "deployment-a", "in", "provider-1", UsageDimensions(1_000, 0, 1_000), 1, reserved.balance.version, "capture-part", "req-part")
        self.assertEqual(partial.reservation.status, ReservationStatus.PARTIALLY_CAPTURED)
        self.assertEqual(partial.reservation.captured_microunits, 1_100)
        released = self.fixture.service.release(EXECUTOR, partial.reservation.reservation_id, partial.reservation.version, partial.balance.version, "release-rest", "req-release")
        self.assertEqual(released.reservation.status, ReservationStatus.RELEASED)
        self.assertEqual(released.balance.reserved_microunits, 0)

        second_fixture = make_billing_fixture(); provision(second_fixture); funded = fund(second_fixture)
        reservation = second_fixture.service.reserve(EXECUTOR, "tenant-billing", Product.AISA, "uwo-general-v1", "req-full", 5_000, FUTURE, funded.balance.version, "reserve-full")
        captured = second_fixture.service.capture(EXECUTOR, reservation.reservation.reservation_id, "usage-full", "azure-openai-in", "deployment-a", "in", "provider-full", UsageDimensions(4_900, 0, 4_900), 1, reservation.balance.version, "capture-full", "req-full")
        self.assertEqual(captured.reservation.status, ReservationStatus.CAPTURED)

    def test_invalid_transitions_overcapture_and_expired_reservation(self) -> None:
        reserved = self._reserve(1_000)
        with self.assertRaises(Conflict) as over:
            self.fixture.service.capture(EXECUTOR, reserved.reservation.reservation_id, "usage-over", "azure-openai-in", None, "in", None, UsageDimensions(1_000, 0, 1_000), 1, reserved.balance.version, "capture-over", "req-over")
        self.assertEqual(over.exception.code, "capture_exceeds_reservation")
        released = self.fixture.service.release(EXECUTOR, reserved.reservation.reservation_id, 1, reserved.balance.version, "release", "req-release")
        with self.assertRaises(Conflict):
            self.fixture.service.release(EXECUTOR, released.reservation.reservation_id, 2, released.balance.version, "release-again", "req-release-again")

        expired_fixture = make_billing_fixture(); provision(expired_fixture); funded = fund(expired_fixture)
        expired_result = expired_fixture.service.reserve(EXECUTOR, "tenant-billing", Product.AISA, "uwo-general-v1", "request-expired", 1_000, FUTURE, funded.balance.version, "reserve-expired")
        expired = replace(expired_result.reservation, expires_at=PAST)
        expired_fixture.reservations.update(expired, expired_result.reservation.version)
        with self.assertRaises(Conflict) as caught:
            expired_fixture.service.capture(EXECUTOR, expired.reservation_id, "usage-expired", "azure-openai-in", None, "in", None, UsageDimensions(1, 0, 1), 1, expired_result.balance.version, "capture-expired", "req-expired")
        self.assertEqual(caught.exception.code, "reservation_expired")
        overridden = expired_fixture.service.capture(PLATFORM, expired.reservation_id, "usage-expired-override", "azure-openai-in", None, "in", None, UsageDimensions(1, 0, 1), 1, expired_result.balance.version, "capture-expired-override", "req-expired-override", True)
        self.assertEqual(overridden.reservation.status, ReservationStatus.PARTIALLY_CAPTURED)
        self.assertTrue(any(event.event_type == "billing.reservation_captured_override" for event in expired_fixture.audit.events))

    def test_tenant_admin_with_billing_read_can_read_only_own_tenant(self) -> None:
        membership = self.fixture.control.service.put_membership(PLATFORM, "tenant-billing", ADMIN_A.subject, MembershipStatus.ACTIVE, 0, "req-member").membership
        self.fixture.control.service.assign_role(PLATFORM, "tenant-billing", ADMIN_A.subject, "tenant-admin", membership.version, "req-role")
        own_identity = replace(ADMIN_A, tenant_id="tenant-billing")
        self.assertEqual(self.fixture.service.read_balance(own_identity, "tenant-billing", "req-own").available_microunits, 0)
        provision(self.fixture, "tenant-other")
        with self.assertRaises(Exception) as caught:
            self.fixture.service.read_balance(own_identity, "tenant-other", "req-cross")
        self.assertEqual(caught.exception.code, "tenant_isolation_violation")

    def test_idempotent_replay_and_conflicting_reuse_do_not_duplicate_ledger(self) -> None:
        funded = fund(self.fixture, amount=1_000)
        adjusted = self.fixture.service.adjust_credits(PLATFORM, "tenant-billing", -100, funded.balance.version, "adjust-after-grant", "req-adjust-after-grant")
        replay = self.fixture.service.grant_credits(PLATFORM, "tenant-billing", 1_000, 1, "grant-tenant-billing-1000", "req-replay")
        self.assertFalse(replay.created)
        self.assertEqual((replay.balance.available_microunits, replay.balance.version), (1_000, 2))
        self.assertEqual(adjusted.balance.available_microunits, 900)
        page = self.fixture.service.list_ledger(PLATFORM, "tenant-billing", 100, None, "req-list")
        self.assertEqual(len(page.items), 2)
        with self.assertRaises(Conflict) as caught:
            self.fixture.service.grant_credits(PLATFORM, "tenant-billing", 2_000, 1, "grant-tenant-billing-1000", "req-conflict")
        self.assertEqual(caught.exception.code, "idempotency_conflict")

    def test_transaction_rollback_removes_partial_state(self) -> None:
        fixture = make_billing_fixture()
        fixture.control.service.create_tenant(PLATFORM, "tenant-rollback", "Rollback", "in", "create-rb", "req-create-rb")
        fixture.failures.fail_next("idempotency_write")
        with self.assertRaises(RepositoryIntegrityError):
            fixture.service.create_account(PLATFORM, "tenant-rollback", "account-rb", "req-rb")
        self.assertIsNone(fixture.accounts.get_by_tenant("tenant-rollback"))
        self.assertEqual(len([event for event in fixture.audit.events if event.event_type == "billing.transaction_rolled_back"]), 1)

        fixture.service.create_account(PLATFORM, "tenant-rollback", "account-rb-2", "req-rb-2")
        fixture.failures.fail_next("ledger_write")
        with self.assertRaises(RepositoryIntegrityError):
            fixture.service.grant_credits(PLATFORM, "tenant-rollback", 100, 1, "grant-rb", "req-grant-rb")
        self.assertEqual(fixture.service.read_balance(PLATFORM, "tenant-rollback", "req-read").available_microunits, 0)

    def test_prevalidation_and_idempotency_conflicts_do_not_emit_rollback_events(self) -> None:
        with self.assertRaises(Exception):
            self.fixture.service.grant_credits(PLATFORM, "tenant-billing", 0, 1, "invalid-grant", "req-invalid-grant")
        fund(self.fixture, amount=100)
        with self.assertRaises(Conflict):
            self.fixture.service.grant_credits(PLATFORM, "tenant-billing", 200, 1, "grant-tenant-billing-100", "req-idempotency-conflict")
        request_ids = {"req-invalid-grant", "req-idempotency-conflict"}
        self.assertEqual([event for event in self.fixture.audit.events if event.request_id in request_ids and event.event_type == "billing.transaction_rolled_back"], [])

    def test_repository_failure_before_first_mutation_does_not_emit_rollback_event(self) -> None:
        self.fixture.control.service.create_tenant(PLATFORM, "tenant-prewrite-failure", "Prewrite", "in", "create-prewrite", "req-create-prewrite")
        with patch.object(self.fixture.idempotency, "get", side_effect=RepositoryIntegrityError("secret prewrite failure")):
            with self.assertRaises(RepositoryIntegrityError):
                self.fixture.service.create_account(PLATFORM, "tenant-prewrite-failure", "account-prewrite", "req-prewrite")
        self.assertEqual([event for event in self.fixture.audit.events if event.request_id == "req-prewrite"], [])

    def test_capture_transaction_rollback_removes_usage_and_state_change(self) -> None:
        reserved = self._reserve()
        self.fixture.failures.fail_next("ledger_write")
        with self.assertRaises(RepositoryIntegrityError):
            self.fixture.service.capture(EXECUTOR, reserved.reservation.reservation_id, "usage-rollback", "azure-openai-in", None, "in", None, UsageDimensions(1_000, 0, 1_000), 1, reserved.balance.version, "capture-rollback", "req-rollback")
        self.assertIsNone(self.fixture.usage.get("usage-rollback"))
        self.assertEqual(self.fixture.reservations.get(reserved.reservation.reservation_id).version, 1)

    def test_concurrent_reservations_cannot_overdraw(self) -> None:
        funded = fund(self.fixture, amount=1_000)
        def reserve(index):
            try:
                return self.fixture.service.reserve(EXECUTOR, "tenant-billing", Product.AISA, "uwo-general-v1", f"req-concurrent-{index}", 800, FUTURE, funded.balance.version, f"concurrent-{index}")
            except (Conflict, PaymentRequired) as exc:
                return exc
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, (1, 2)))
        self.assertEqual(sum(not isinstance(item, Exception) for item in results), 1)
        balance = self.fixture.service.read_balance(PLATFORM, "tenant-billing", "req-after")
        self.assertGreaterEqual(balance.available_microunits, 0)

    def test_suspended_tenant_and_closed_account_deny_debits(self) -> None:
        fund(self.fixture)
        tenant = self.fixture.control.tenants.get("tenant-billing")
        self.fixture.control.service.set_tenant_status(PLATFORM, "tenant-billing", TenantStatus.SUSPENDED, tenant.version, "req-suspend-tenant")
        with self.assertRaises(Exception) as caught:
            self.fixture.service.authorize_estimated_charge(EXECUTOR, "tenant-billing", 1, "req-suspended")
        self.assertEqual(caught.exception.code, "tenant_suspended")

        second = make_billing_fixture(); provision(second); fund(second)
        second.service.set_account_status(PLATFORM, "tenant-billing", BillingAccountStatus.CLOSED, 1, "req-close")
        with self.assertRaises(Conflict):
            second.service.authorize_estimated_charge(EXECUTOR, "tenant-billing", 1, "req-closed")

    def test_rate_card_version_and_redacted_usage_are_preserved(self) -> None:
        reserved = self._reserve()
        captured = self.fixture.service.capture(EXECUTOR, reserved.reservation.reservation_id, "usage-redacted", "azure-openai-in", "deployment-a", "in", "provider-response", UsageDimensions(1, 1, 2), 1, reserved.balance.version, "capture-redacted", "req-redacted")
        self.fixture.rate_cards.add(example_rate_card(2, "2026-07-20T12:30:00+00:00", 10))
        stored = self.fixture.usage.get("usage-redacted")
        self.assertEqual(stored.rate_card_version, 1)
        serialized = json.dumps(stored.__dict__, default=lambda value: value.__dict__)
        for forbidden in ("prompt", "model_output", "api_key", "bearer"):
            self.assertNotIn(forbidden, serialized.lower())

    def test_capture_selects_rate_card_effective_at_usage_occurrence(self) -> None:
        later = "2026-07-20T12:30:00+00:00"
        after = "2026-07-20T12:31:00+00:00"
        current = [NOW]
        fixture = make_billing_fixture(clock=lambda: current[0], rate_card_values=(example_rate_card(1, NOW), example_rate_card(2, later, 2)))
        provision(fixture)
        funded = fund(fixture)
        first = fixture.service.reserve(EXECUTOR, "tenant-billing", Product.AISA, "uwo-general-v1", "req-rate-before", 5_000, FUTURE, funded.balance.version, "reserve-rate-before")
        before_capture = fixture.service.capture(EXECUTOR, first.reservation.reservation_id, "usage-rate-before", "azure-openai-in", None, "in", None, UsageDimensions(1, 0, 1), 1, first.balance.version, "capture-rate-before", "req-rate-before")
        self.assertEqual(before_capture.usage_event.rate_card_version, 1)

        current[0] = after
        balance = fixture.service.read_balance(PLATFORM, "tenant-billing", "req-rate-balance")
        second = fixture.service.reserve(EXECUTOR, "tenant-billing", Product.AISA, "uwo-general-v1", "req-rate-after", 5_000, FUTURE, balance.version, "reserve-rate-after")
        after_capture = fixture.service.capture(EXECUTOR, second.reservation.reservation_id, "usage-rate-after", "azure-openai-in", None, "in", None, UsageDimensions(1, 0, 1), 1, second.balance.version, "capture-rate-after", "req-rate-after")
        self.assertEqual(after_capture.usage_event.rate_card_version, 2)
        self.assertGreater(after_capture.charge.total_charge_microunits, before_capture.charge.total_charge_microunits)

    def test_ledger_pagination(self) -> None:
        first = fund(self.fixture, amount=100)
        self.fixture.service.grant_credits(PLATFORM, "tenant-billing", 200, first.balance.version, "grant-two", "req-two")
        page = self.fixture.service.list_ledger(PLATFORM, "tenant-billing", 1, None, "req-page")
        self.assertEqual(len(page.items), 1)
        self.assertIsNotNone(page.next_cursor)


if __name__ == "__main__":
    unittest.main()
