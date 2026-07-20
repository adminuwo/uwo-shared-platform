from dataclasses import dataclass

from packages.contracts import MembershipStatus, VerifiedSubjectIdentity
from services.platform_control_plane.audit import ControlPlaneAuditEvent
from services.platform_control_plane.auth import AuthenticationError
from services.platform_control_plane.authorization import ControlPlaneAuthorizer, StaticSubjectDirectory
from services.platform_control_plane.in_memory import (
    InMemoryEntitlementRepository,
    InMemoryMembershipRepository,
    InMemoryPolicyVersionRepository,
    InMemoryRoleRepository,
    InMemoryTenantRepository,
)
from services.platform_control_plane.service import PlatformControlPlane, built_in_roles

NOW = "2026-07-20T12:00:00+00:00"
PLATFORM = VerifiedSubjectIdentity("platform-admin", "platform", NOW)
ADMIN_A = VerifiedSubjectIdentity("admin-a", "tenant-a", NOW)
ADMIN_B = VerifiedSubjectIdentity("admin-b", "tenant-b", NOW)
USER_A = VerifiedSubjectIdentity("user-a", "tenant-a", NOW)


class CaptureAudit:
    def __init__(self) -> None:
        self.events: list[ControlPlaneAuditEvent] = []

    def emit(self, event: ControlPlaneAuditEvent) -> None:
        self.events.append(event)


class HeaderAuthenticator:
    def __init__(self) -> None:
        self._identities = {
            "Bearer platform": PLATFORM,
            "Bearer admin-a": ADMIN_A,
            "Bearer admin-b": ADMIN_B,
            "Bearer user-a": USER_A,
        }

    def authenticate(self, authorization: str) -> VerifiedSubjectIdentity:
        identity = self._identities.get(authorization)
        if identity is None:
            raise AuthenticationError("invalid_token", "trusted bearer assertion is required")
        return identity


@dataclass
class ControlPlaneFixture:
    service: PlatformControlPlane
    tenants: InMemoryTenantRepository
    memberships: InMemoryMembershipRepository
    roles: InMemoryRoleRepository
    entitlements: InMemoryEntitlementRepository
    policies: InMemoryPolicyVersionRepository
    audit: CaptureAudit


def make_fixture() -> ControlPlaneFixture:
    tenants = InMemoryTenantRepository()
    memberships = InMemoryMembershipRepository()
    roles = InMemoryRoleRepository(built_in_roles(NOW))
    entitlements = InMemoryEntitlementRepository()
    policies = InMemoryPolicyVersionRepository()
    audit = CaptureAudit()
    authorizer = ControlPlaneAuthorizer(tenants, memberships, roles, frozenset({PLATFORM.subject}))
    service = PlatformControlPlane(
        tenants,
        memberships,
        roles,
        entitlements,
        policies,
        StaticSubjectDirectory(frozenset({PLATFORM.subject, ADMIN_A.subject, ADMIN_B.subject, USER_A.subject})),
        authorizer,
        audit,
        clock=lambda: NOW,
    )
    return ControlPlaneFixture(service, tenants, memberships, roles, entitlements, policies, audit)


def bootstrap_tenant_admin(fixture: ControlPlaneFixture, tenant_id: str, admin: VerifiedSubjectIdentity) -> None:
    fixture.service.create_tenant(PLATFORM, tenant_id, f"Tenant {tenant_id}", "in", f"create-{tenant_id}", f"req-create-{tenant_id}")
    membership = fixture.service.put_membership(PLATFORM, tenant_id, admin.subject, MembershipStatus.ACTIVE, 0, f"req-member-{tenant_id}").membership
    fixture.service.assign_role(PLATFORM, tenant_id, admin.subject, "tenant-admin", membership.version, f"req-role-{tenant_id}")
