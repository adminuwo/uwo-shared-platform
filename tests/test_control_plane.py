import json
import unittest

from packages.contracts import MembershipStatus, Product, TenantStatus
from services.platform_control_plane.entitlements import RepositoryEntitlementLookup
from services.platform_control_plane.errors import AuthorizationDenied, Conflict, ResourceNotFound, StaleVersion

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
            self.fixture.service.create_tenant(PLATFORM, "tenant-b", "Tenant B", "in", "same-key", "req-2")
        self.assertEqual(caught.exception.code, "idempotency_conflict")

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

    def test_audit_events_are_redacted_and_denials_are_recorded(self) -> None:
        bootstrap_tenant_admin(self.fixture, "tenant-a", ADMIN_A)
        secret = "Bearer secret-token-that-must-not-appear"
        with self.assertRaises(AuthorizationDenied):
            self.fixture.service.get_tenant(ADMIN_B, "tenant-a", "req-denied")
        serialized = json.dumps([event.__dict__ for event in self.fixture.audit.events])
        self.assertNotIn(secret, serialized)
        self.assertTrue(any(event.event_type == "administration.denied" and event.outcome == "denied" for event in self.fixture.audit.events))


if __name__ == "__main__":
    unittest.main()
