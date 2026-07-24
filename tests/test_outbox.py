import unittest
from concurrent.futures import ThreadPoolExecutor

from services.data_service_common import (
    CollectingEventPublisher,
    Conflict,
    InMemoryOutbox,
    OutboxDispatcher,
    OutboxEventRecorder,
    OutboxRecord,
    OutboxStatus,
    deterministic_id,
    platform_event,
)

NOW = "2026-07-20T12:00:00+00:00"
LATER = "2026-07-20T12:00:31+00:00"


class _FailOncePublisher:
    def __init__(self, downstream):
        self.downstream = downstream
        self.failed = False

    def publish(self, event):
        if not self.failed:
            self.failed = True
            raise RuntimeError("injected transport failure")
        self.downstream.publish(event)


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.outbox = InMemoryOutbox()
        self.event = platform_event(
            "storage.object.finalized",
            "tenant-a",
            "request-1",
            {"resource_id": "object-1", "region": "in", "product": "aisa"},
            NOW,
        )
        self.record = OutboxEventRecorder(self.outbox, max_attempts=2).record(self.event)
        self.record_id = deterministic_id("outbox", self.event.event_id)

    def test_concurrent_claim_has_one_winner(self):
        def claim(owner):
            try:
                return self.outbox.claim(self.record_id, owner, NOW, 30)
            except Conflict as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ("worker-a", "worker-b")))
        self.assertEqual(sum(not isinstance(result, str) for result in results), 1)
        self.assertEqual(sum(result == "outbox_already_claimed" for result in results), 1)

    def test_lease_expiry_allows_recovery(self):
        first = self.outbox.claim(self.record_id, "worker-a", NOW, 30)
        with self.assertRaises(Conflict):
            self.outbox.claim(self.record_id, "worker-b", NOW, 30)
        with self.assertRaises(Conflict) as expired:
            self.outbox.acknowledge(self.record_id, "worker-a", first.version, LATER)
        self.assertEqual(expired.exception.code, "outbox_lease_expired")
        recovered = self.outbox.claim(self.record_id, "worker-b", LATER, 30)
        self.assertEqual(recovered.attempts, first.attempts + 1)
        self.assertEqual(recovered.lease_owner, "worker-b")

    def test_retry_due_time_is_enforced(self):
        claimed = self.outbox.claim(self.record_id, "worker-a", NOW, 30)
        retry = self.outbox.retry(self.record_id, "worker-a", claimed.version, LATER)
        with self.assertRaises(Conflict) as denied:
            self.outbox.claim(self.record_id, "worker-a", NOW, 30)
        self.assertEqual(denied.exception.code, "retry_not_due")
        self.assertEqual(self.outbox.claim(self.record_id, "worker-a", LATER, 30).attempts, retry.attempts + 1)

    def test_duplicate_acknowledgement_is_idempotent(self):
        claimed = self.outbox.claim(self.record_id, "worker-a", NOW, 30)
        first = self.outbox.acknowledge(self.record_id, "worker-a", claimed.version)
        second = self.outbox.acknowledge(self.record_id, "worker-a", claimed.version)
        self.assertEqual(first, second)
        self.assertEqual(second.status, OutboxStatus.ACCEPTED)

    def test_poison_event_is_dead_lettered_at_maximum_attempts(self):
        claimed = self.outbox.claim(self.record_id, "worker-a", NOW, 30)
        pending = self.outbox.retry(self.record_id, "worker-a", claimed.version, LATER)
        claimed_again = self.outbox.claim(self.record_id, "worker-a", LATER, 30)
        dead = self.outbox.retry(self.record_id, "worker-a", claimed_again.version, "2026-07-20T12:01:02+00:00")
        self.assertEqual(pending.status, OutboxStatus.PENDING)
        self.assertEqual(dead.status, OutboxStatus.DEAD_LETTERED)

    def test_exhausted_expired_claim_requires_owner_reconciliation(self):
        first = self.outbox.claim(self.record_id, "worker-a", NOW, 30)
        self.outbox.retry(self.record_id, "worker-a", first.version, LATER)
        final = self.outbox.claim(self.record_id, "worker-b", LATER, 30)
        after_final_lease = "2026-07-20T12:01:02+00:00"
        with self.assertRaises(Conflict) as denied:
            self.outbox.claim(self.record_id, "worker-c", after_final_lease, 30)
        self.assertEqual(denied.exception.code, "outbox_attempts_exhausted")
        current = self.outbox.get(self.record_id)
        self.assertEqual(current.status, OutboxStatus.CLAIMED)
        self.assertEqual(current.version, final.version)

    def test_event_id_includes_safe_attribute_fingerprint(self):
        other = platform_event(
            self.event.event_type,
            self.event.tenant_id,
            self.event.request_id,
            {"resource_id": "object-2", "region": "in", "product": "aisa"},
            NOW,
        )
        self.assertNotEqual(self.event.event_id, other.event_id)
        with self.assertRaises(ValueError):
            platform_event("provider.execution", "tenant-a", "request-2", {"prompt": "sensitive"}, NOW)

    def test_dispatch_recovers_across_dispatcher_restart(self):
        downstream = CollectingEventPublisher()
        publisher = _FailOncePublisher(downstream)
        first_process = OutboxDispatcher(self.outbox, publisher, "worker-a")
        retry = first_process.dispatch(self.record_id, NOW, LATER)
        self.assertEqual(retry.status, OutboxStatus.PENDING)
        second_process = OutboxDispatcher(self.outbox, publisher, "worker-b")
        accepted = second_process.dispatch(self.record_id, LATER, "2026-07-20T12:02:00+00:00")
        self.assertEqual(accepted.status, OutboxStatus.ACCEPTED)
        self.assertEqual(downstream.events, (self.event,))


if __name__ == "__main__":
    unittest.main()
