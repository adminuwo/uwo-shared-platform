import json
import unittest

from packages.contracts import MembershipStatus, Product, TenantStatus
from services.platform_control_plane.entitlements import RepositoryEntitlementLookup
from services.platform_control_plane.errors import AuthorizationDenied, Conflict, RepositoryIntegrityError, ResourceNotFound, StaleVersion
from services.platform_control_plane.repositories import IdempotencyScope

from control_plane_support import ADMIN_A, ADMIN_B, PLATFORM, USER_A, bootstrap_tenant_admin, make_fixture


class PlatformControlPlaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = make_fixture()

    def test_tenant_creation_retrieval_and_policy_version(self) -> None:
        result = self.fixture.service.create_tenant(PLATFORM, "tenant-a", "Tenant A", "in", "idem-a", "req-a")
        self.assertTrue(result.created)
        self.assertEqual(self.fixture.service.get_tenant(PLATFORM, "tenant-a", "req-get").tenant_id, "tenant-a")
        self.assertEqual(self.fixture.service.policy_version(PLATFORM, "tenant-a", "req-policy").policy_version, 1)

    def test_duplicate_tenant_is_rejected_but_same_key_is_idempotent(self) -> None:
        first = self.fixture.service.create_tenant(PLATFORM, "tenant-a", "Tenant A", "in", "idem-a", "req-1")
        repeated = self.fixture.service.create_tenant(PLATFORM, "tenant-a", "Tenant A", "in", "idem-a", "req-2")
        self.assertTrue(first.created)
        self.assertFalse(repeated.created)
        with self.assertRaises(Conflict) as caught:
            self.fixture.service.create_tenant(PLATFORM, "tenant-a", "Tenant A", "in", "another-key", "req-3")
        self.assertEqual(caught.exception.code, "tenant_exists")

    def test_idempotency_key_cannot_be_reused_for_different_create(self) -> None:
        self.fixture.service.create_tenant(PLATFORM, "tenant-a", "Tenant A", "in", "same-key", "req-1")
        with self.assertRaises(Conflict) as caught:
            self.fixture.service.create_tenant(PLATFORM, "tenant-a", "Changed Name", "in", "same-key", "req-2")
        self.assertEqual(caught.exception.code, "idempotency_conflict")

    def test_tenant_create_replay_returns_original_snapshot_after_status_change(self) -> None:
        created = self.fixture.service.create_tenant(PLATFORM, "tenant-a", "Tenant A", "in", "create-a", "req-create")
        self.fixture.service.set_tenant_status(PLATFORM, "tenant-a", TenantStatus.SUSPENDED, 1, "req-suspend")
        replay = self.fixture.service.create_tenant(PLATFORM, "tenant-a", "Tenant A", "in", "create-a", "req-replay")
        self.assertTrue(created.created)
        self.assertFalse(replay.created)
        self.assertEqual((replay.tenant.version, replay.tenant.status), (1, TenantStatus.ACTIVE))
        self.assertEqual((self.fixture.tenants.get("tenant-a").version, self.fixture.tenants.get("tenant-a").status), (2, TenantStatus.SUSPENDED))
        record = self.fixture.idempotency.get(IdempotencyScope("tenant.create", "tenant-a", PLATFORM.subject), "create-a")
        self.assertIsNot(record.original_result, created.tenant)
        events = [event for event in self.fixture.audit.events if event.event_type == "tenant.created"]
        self.assertEqual(len(events), 1)

    def test_idempotency_key_is_scoped_by_operation_tenant_and_actor(self) -> None:
        self.fixture.service.create_tenant(PLATFORM, "tenant-a", "Tenant A", "in", "shared-key", "req-create")
        self.fixture.service.create_tenant(PLATFORM, "tenant-b", "Tenant B", "in", "shared-key", "req-create-b")
        product = self.fixture.service.grant_product(PLATFORM, "tenant-a", Product.AISA, 1, "shared-key", "req-product")
        membership = self.fixture.service.put_membership(PLATFORM, "tenant-a", ADMIN_A.subject, MembershipStatus.ACTIVE, 0, "req-admin-member").membership
        self.fixture.service.assign_role(PLATFORM, "tenant-a", ADMIN_A.subject, "tenant-admin", membership.version, "req-admin-role")
        actor_scoped = self.fixture.service.grant_product(ADMIN_A, "tenant-a", Product.AI_MALL, 2, "shared-key", "req-actor-product")
        model = self.fixture.service.grant_model(PLATFORM, "tenant-a", "uwo-general-v1", 3, "shared-key", "req-model")
        self.assertTrue(product.created)
        self.assertTrue(actor_scoped.created)
        self.assertTrue(model.created)

    def test_entitlement_idempotency_conflict_fails_before_mutation(self) -> None:
        self.fixture.service.create_tenant(PLATFORM, "tenant-a", "Tenant A", "in", "create-a", "req-create")
        self.fixture.service.grant_product(PLATFORM, "tenant-a", Product.AISA, 1, "grant-key", "req-grant")
        with self.assertRaises(Conflict) as caught:
            self.fixture.service.grant_product(PLATFORM, "tenant-a", Product.AI_MALL, 2, "grant-key", "req-conflict")
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        self.assertEqual([item.product for item in self.fixture.entitlements.snapshot("tenant-a").products], [Product.AISA])

    def test_tenant_provisioning_failures_roll_back_every_resource(self) -> None:
        for failure_point in ("tenant_write", "entitlement_initialization", "policy_initialization", "idempotency_persistence"):
            with self.subTest(failure_point=failure_point):
                fixture = make_fixture()
                fixture.failures.fail_next(failure_point)
                with self.assertRaises(RepositoryIntegrityError):
                    fixture.service.create_tenant(PLATFORM, "tenant-failed", "Failed Tenant", "in", "failed-key", "req-failed")
                self.assertIsNone(fixture.tenants.get("tenant-failed"))
                with self.assertRaises(ResourceNotFound):
                    fixture.entitlements.snapshot("tenant-failed")
                self.assertIsNone(fixture.policies.current("tenant-failed"))
                scope = IdempotencyScope("tenant.create", "tenant-failed", PLATFORM.subject)
                self.assertIsNone(fixture.idempotency.get(scope, "failed-key"))
                rollback_events = [event for event in fixture.audit.events if event.event_type == "tenant.provisioning_rolled_back"]
                self.assertEqual(len(rollback_events), 1)

    def test_membership_creation_update_and_duplicate_rejection(self) -> None:
        self.fixture.service.create_tenant(PLATFORM, "tenant-a", "Tenant A", "in", "create-a", "req-1")
        created = self.fixture.service.put_membership(PLATFORM, "tenant-a", USER_A.subject, MembershipStatus.ACTIVE, 0, "req-2")
        self.assertTrue(created.created)
        with self.assertRaises(Conflict):
            self.fixture.service.put_membership(PLATFORM, "tenant-a", USER_A.subject, MembershipStatus.ACTIVE, 0, "req-3")
        updated = self.fixture.service.put_membership(PLATFORM, "tenant-a", USER_A.subject, MembershipStatus.SUSPENDED, created.membership.version, "req-4")
        self.assertFalse(updated.created)
        self.assertEqual(updated.membership.status, MembershipStatus.SUSPENDED)

    def test_unknown_subject_membership_fails_closed(self) -> None:
        self.fixture.service.create_tenant(PLATFORM, "tenant-a", "Tenant A", "in", "create-a", "req-1")
        with self.assertRaises(ResourceNotFound) as caught:
            self.fixture.service.put_membership(PLATFORM, "tenant-a", "unknown-user", MembershipStatus.ACTIVE, 0, "req-2")
        self.assertEqual(caught.exception.code, "unknown_subject")

    def test_unknown_tenant_mutation_fails_closed_for_platform_admin(self) -> None:
        with self.assertRaises(ResourceNotFound) as caught:
            self.fixture.service.put_membership(PLATFORM, "unknown-tenant", USER_A.subject, MembershipStatus.ACTIVE, 0, "req-unknown-tenant")
        self.assertEqual(caught.exception.code, "unknown_tenant")

    def test_role_assignment_revocation_and_effective_permissions(self) -> None:
        self.fixture.service.create_tenant(PLATFORM, "tenant-a", "Tenant A", "in", "create-a", "req-1")
        membership = self.fixture.service.put_membership(PLATFORM, "tenant-a", USER_A.subject, MembershipStatus.ACTIVE, 0, "req-2").membership
        assigned = self.fixture.service.assign_role(PLATFORM, "tenant-a", USER_A.subject, "tenant-reader", membership.version, "req-3")
        permissions = self.fixture.service.effective_permissions(PLATFORM, "tenant-a", USER_A.subject, "req-4")
        self.assertEqual(permissions, tuple(sorted(("entitlement.read", "policy.read", "tenant.read"))))
        revoked = self.fixture.service.revoke_role(PLATFORM, "tenant-a", USER_A.subject, "tenant-reader", assigned.version, "req-5")
        self.assertEqual(revoked.role_ids, ())

    def test_duplicate_and_unknown_role_assignments_fail_closed(self) -> None:
        bootstrap_tenant_admin(self.fixture, "tenant-a", ADMIN_A)
        membership = self.fixture.memberships.get("tenant-a", ADMIN_A.subject)
        assert membership is not None
        with self.assertRaises(Conflict):
            self.fixture.service.assign_role(PLATFORM, "tenant-a", ADMIN_A.subject, "tenant-admin", membership.version, "req-dup")
        with self.assertRaises(ResourceNotFound):
            self.fixture.service.assign_role(PLATFORM, "tenant-a", ADMIN_A.subject, "unknown-role", membership.version, "req-unknown")

    def test_product_and_model_entitlements_are_deterministic_and_idempotent(self) -> None:
        bootstrap_tenant_admin(self.fixture, "tenant-a", ADMIN_A)
        product = self.fixture.service.grant_product(ADMIN_A, "tenant-a", Product.AISA, 1, "grant-product", "req-product")
        self.assertTrue(product.created)
        repeated = self.fixture.service.grant_product(ADMIN_A, "tenant-a", Product.AISA, 1, "grant-product", "req-repeat")
        self.assertFalse(repeated.created)
        model = self.fixture.service.grant_model(ADMIN_A, "tenant-a", "uwo-general-v1", 2, "grant-model", "req-model")
        self.assertTrue(model.created)
        snapshot = self.fixture.service.effective_entitlements(ADMIN_A, "tenant-a", "req-read")
        self.assertEqual(([item.product.value for item in snapshot.products], [item.model for item in snapshot.models]), (["aisa"], ["uwo-general-v1"]))
        snapshot = self.fixture.service.revoke_product(ADMIN_A, "tenant-a", Product.AISA, 3, "req-revoke-product")
        self.assertEqual(snapshot.products, ())
        snapshot = self.fixture.service.revoke_model(ADMIN_A, "tenant-a", "uwo-general-v1", 4, "req-revoke-model")
        self.assertEqual(snapshot.models, ())

    def test_entitlement_grant_replay_survives_revocation(self) -> None:
        bootstrap_tenant_admin(self.fixture, "tenant-a", ADMIN_A)
        granted = self.fixture.service.grant_product(ADMIN_A, "tenant-a", Product.AISA, 1, "grant-product", "req-grant")
        self.fixture.service.revoke_product(ADMIN_A, "tenant-a", Product.AISA, 2, "req-revoke")
        replay = self.fixture.service.grant_product(ADMIN_A, "tenant-a", Product.AISA, 1, "grant-product", "req-replay")
        self.assertFalse(replay.created)
        self.assertEqual(replay.entitlement, granted.entitlement)
        self.assertEqual(replay.entitlement.version, 2)
        record = self.fixture.idempotency.get(IdempotencyScope("entitlement.product.grant", "tenant-a", ADMIN_A.subject), "grant-product")
        self.assertIsNot(record.original_result, granted.entitlement)
        self.assertEqual(self.fixture.entitlements.snapshot("tenant-a").products, ())
        events = [event for event in self.fixture.audit.events if event.event_type == "entitlement.product_granted"]
        self.assertEqual(len(events), 1)

    def test_provider_neutral_entitlement_lookup_fails_closed(self) -> None:
        bootstrap_tenant_admin(self.fixture, "tenant-a", ADMIN_A)
        self.fixture.service.grant_product(ADMIN_A, "tenant-a", Product.AISA, 1, "product", "req-product")
        self.fixture.service.grant_model(ADMIN_A, "tenant-a", "uwo-general-v1", 2, "model", "req-model")
        lookup = RepositoryEntitlementLookup(self.fixture.tenants, self.fixture.entitlements)
        lookup.authorize("tenant-a", Product.AISA, "uwo-general-v1")
        with self.assertRaises(AuthorizationDenied):
            lookup.authorize("tenant-a", Product.AI_MALL, "uwo-general-v1")

    def test_tenant_isolation_and_unauthorized_administration(self) -> None:
        bootstrap_tenant_admin(self.fixture, "tenant-a", ADMIN_A)
        bootstrap_tenant_admin(self.fixture, "tenant-b", ADMIN_B)
        with self.assertRaises(AuthorizationDenied) as isolated:
            self.fixture.service.get_tenant(ADMIN_A, "tenant-b", "req-cross")
        self.assertEqual(isolated.exception.code, "tenant_isolation_violation")
        with self.assertRaises(AuthorizationDenied):
            self.fixture.service.create_tenant(ADMIN_A, "tenant-c", "Tenant C", "in", "create-c", "req-create-c")

    def test_deprovisioned_target_subject_cannot_receive_or_revoke_role(self) -> None:
        bootstrap_tenant_admin(self.fixture, "tenant-a", ADMIN_A)
        membership = self.fixture.service.put_membership(ADMIN_A, "tenant-a", USER_A.subject, MembershipStatus.ACTIVE, 0, "req-member").membership
        self.fixture.subjects.deprovision(USER_A.subject)
        with self.assertRaises(AuthorizationDenied) as assigned:
            self.fixture.service.assign_role(ADMIN_A, "tenant-a", USER_A.subject, "tenant-reader", membership.version, "req-assign")
        self.assertEqual(assigned.exception.code, "deprovisioned_subject")
        with self.assertRaises(AuthorizationDenied) as revoked:
            self.fixture.service.revoke_role(ADMIN_A, "tenant-a", USER_A.subject, "tenant-reader", membership.version, "req-revoke")
        self.assertEqual(revoked.exception.code, "deprovisioned_subject")

    def test_deprovisioned_member_has_no_effective_permissions(self) -> None:
        bootstrap_tenant_admin(self.fixture, "tenant-a", ADMIN_A)
        membership = self.fixture.service.put_membership(ADMIN_A, "tenant-a", USER_A.subject, MembershipStatus.ACTIVE, 0, "req-member").membership
        self.fixture.service.assign_role(ADMIN_A, "tenant-a", USER_A.subject, "tenant-reader", membership.version, "req-role")
        self.fixture.subjects.deprovision(USER_A.subject)
        with self.assertRaises(AuthorizationDenied) as caught:
            self.fixture.service.effective_permissions(PLATFORM, "tenant-a", USER_A.subject, "req-permissions")
        self.assertEqual(caught.exception.code, "deprovisioned_subject")

    def test_deprovisioned_tenant_admin_immediately_loses_authority(self) -> None:
        bootstrap_tenant_admin(self.fixture, "tenant-a", ADMIN_A)
        self.fixture.subjects.deprovision(ADMIN_A.subject)
        with self.assertRaises(AuthorizationDenied) as caught:
            self.fixture.service.grant_product(ADMIN_A, "tenant-a", Product.AISA, 1, "grant", "req-grant")
        self.assertEqual(caught.exception.code, "deprovisioned_subject")

    def test_stale_versions_are_rejected(self) -> None:
        bootstrap_tenant_admin(self.fixture, "tenant-a", ADMIN_A)
        with self.assertRaises(StaleVersion):
            self.fixture.service.set_tenant_status(ADMIN_A, "tenant-a", TenantStatus.SUSPENDED, 99, "req-stale")
        with self.assertRaises(StaleVersion):
            self.fixture.service.grant_product(ADMIN_A, "tenant-a", Product.AISA, 99, "grant", "req-grant")

    def test_suspended_tenant_blocks_admin_and_entitlement_lookup(self) -> None:
        bootstrap_tenant_admin(self.fixture, "tenant-a", ADMIN_A)
        suspended = self.fixture.service.set_tenant_status(ADMIN_A, "tenant-a", TenantStatus.SUSPENDED, 1, "req-suspend")
        self.assertEqual(suspended.status, TenantStatus.SUSPENDED)
        with self.assertRaises(AuthorizationDenied) as blocked:
            self.fixture.service.grant_product(ADMIN_A, "tenant-a", Product.AISA, 1, "grant", "req-grant")
        self.assertEqual(blocked.exception.code, "tenant_suspended")
        with self.assertRaises(AuthorizationDenied):
            RepositoryEntitlementLookup(self.fixture.tenants, self.fixture.entitlements).get_effective_entitlements("tenant-a")
        with self.assertRaises(AuthorizationDenied):
            self.fixture.service.set_tenant_status(ADMIN_A, "tenant-a", TenantStatus.ACTIVE, 2, "req-reactivate")
        self.assertEqual(self.fixture.service.set_tenant_status(PLATFORM, "tenant-a", TenantStatus.ACTIVE, 2, "req-platform-reactivate").status, TenantStatus.ACTIVE)

    def test_success_audit_events_are_redacted(self) -> None:
        bootstrap_tenant_admin(self.fixture, "tenant-a", ADMIN_A)
        secret = "Bearer secret-token-that-must-not-appear"
        serialized = json.dumps([event.__dict__ for event in self.fixture.audit.events])
        self.assertNotIn(secret, serialized)


if __name__ == "__main__":
    unittest.main()
