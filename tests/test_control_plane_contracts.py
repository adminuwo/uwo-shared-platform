import unittest

from packages.contracts import MembershipStatus, Permission, Tenant, TenantMembership, TenantStatus, VerifiedSubjectIdentity
from services.platform_control_plane.service import built_in_roles

from control_plane_support import NOW


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


if __name__ == "__main__":
    unittest.main()
