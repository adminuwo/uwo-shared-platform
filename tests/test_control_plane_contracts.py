import unittest

from packages.contracts import MembershipStatus, Permission, PolicyDocument, Tenant, TenantMembership, TenantStatus, VerifiedSubjectIdentity
from services.platform_control_plane.service import built_in_roles

from control_plane_support import NOW, PLATFORM, make_fixture


class ControlPlaneContractTests(unittest.TestCase):
    def test_contracts_expose_schema_versions_and_utc_timestamps(self) -> None:
        tenant = Tenant("tenant-a", "Tenant A", TenantStatus.ACTIVE, "in", NOW, NOW, 1)
        identity = VerifiedSubjectIdentity("subject-a", "tenant-a", NOW)
        membership = TenantMembership("membership-a", "tenant-a", "subject-a", MembershipStatus.ACTIVE, (), NOW, NOW, 1)
        self.assertEqual((tenant.schema_version, identity.schema_version, membership.schema_version), ("1", "1", "1"))

    def test_non_utc_timestamp_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Tenant("tenant-a", "Tenant A", TenantStatus.ACTIVE, "in", "2026-07-20T12:00:00+05:30", NOW, 1)

    def test_unstable_identifier_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Tenant("tenant/a", "Tenant A", TenantStatus.ACTIVE, "in", NOW, NOW, 1)

    def test_builtin_role_permissions_are_known_and_sorted(self) -> None:
        admin = next(role for role in built_in_roles(NOW) if role.role_id == "tenant-admin")
        self.assertEqual(admin.permission_ids, tuple(sorted(item.value for item in Permission)))

    def test_policy_is_deeply_immutable(self) -> None:
        source = {"nested": {"items": [{"enabled": True}, 2]}, "label": "policy"}
        document = PolicyDocument("tenant-a", 1, source, NOW, PLATFORM.subject)
        source["nested"]["items"][0]["enabled"] = False
        self.assertTrue(document.policy["nested"]["items"][0]["enabled"])
        with self.assertRaises(TypeError):
            document.policy["new"] = "value"
        with self.assertRaises(TypeError):
            document.policy["nested"]["items"][0]["enabled"] = False
        self.assertIsInstance(document.policy["nested"]["items"], tuple)

    def test_invalid_policy_values_are_rejected(self) -> None:
        invalid_values = (
            {1: "non-string key"},
            {"value": float("nan")},
            {"value": float("inf")},
            {"value": object()},
            {"value": ("tuple-is-not-json",)},
        )
        for value in invalid_values:
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    PolicyDocument("tenant-a", 1, value, NOW, PLATFORM.subject)

    def test_policy_serialization_and_fingerprint_are_deterministic(self) -> None:
        first = PolicyDocument("tenant-a", 1, {"z": [3, {"b": 2, "a": 1}], "a": True}, NOW, PLATFORM.subject)
        second = PolicyDocument("tenant-a", 1, {"a": True, "z": [3, {"a": 1, "b": 2}]}, NOW, PLATFORM.subject)
        self.assertEqual(first.canonical_policy(), '{"a":true,"z":[3,{"a":1,"b":2}]}')
        self.assertEqual(first.canonical_policy(), second.canonical_policy())
        self.assertEqual(first.policy_fingerprint(), second.policy_fingerprint())

    def test_tenant_status_is_separate_from_policy_configuration(self) -> None:
        fixture = make_fixture()
        fixture.service.create_tenant(PLATFORM, "tenant-a", "Tenant A", "in", "create-a", "req-create")
        before = fixture.service.policy_version(PLATFORM, "tenant-a", "req-policy-before")
        fixture.service.set_tenant_status(PLATFORM, "tenant-a", TenantStatus.SUSPENDED, 1, "req-suspend")
        after = fixture.service.policy_version(PLATFORM, "tenant-a", "req-policy-after")
        self.assertNotIn("status", before.policy)
        self.assertEqual(before, after)
        self.assertEqual(fixture.tenants.get("tenant-a").status, TenantStatus.SUSPENDED)


if __name__ == "__main__":
    unittest.main()
