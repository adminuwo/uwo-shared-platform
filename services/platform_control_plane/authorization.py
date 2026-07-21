"""Platform-admin and tenant-admin authorization with tenant isolation."""

from __future__ import annotations

from typing import Protocol

from packages.contracts import MembershipStatus, Permission, TenantStatus, VerifiedSubjectIdentity

from .errors import AuthorizationDenied, ResourceNotFound
from .repositories import MembershipRepository, RoleRepository, TenantRepository


class SubjectDirectory(Protocol):
    def exists(self, subject: str) -> bool: ...


class StaticSubjectDirectory:
    """Explicit subject catalog for tests and controlled internal integration."""

    def __init__(self, subjects: frozenset[str]) -> None:
        self._subjects = set(subjects)

    def exists(self, subject: str) -> bool:
        return subject in self._subjects

    def deprovision(self, subject: str) -> None:
        self._subjects.discard(subject)

    def provision(self, subject: str) -> None:
        self._subjects.add(subject)


class ControlPlaneAuthorizer:
    def __init__(
        self,
        tenants: TenantRepository,
        memberships: MembershipRepository,
        roles: RoleRepository,
        subjects: SubjectDirectory,
        platform_admin_subjects: frozenset[str],
    ) -> None:
        self._tenants = tenants
        self._memberships = memberships
        self._roles = roles
        self._subjects = subjects
        self._platform_admin_subjects = platform_admin_subjects

    def is_platform_admin(self, identity: VerifiedSubjectIdentity) -> bool:
        return identity.subject in self._platform_admin_subjects

    def require_platform_admin(self, identity: VerifiedSubjectIdentity) -> None:
        if not self._subjects.exists(identity.subject):
            raise AuthorizationDenied("deprovisioned_subject", "subject is no longer active in the identity directory")
        if not self.is_platform_admin(identity):
            raise AuthorizationDenied("platform_admin_required", "platform administrator authorization is required")

    def effective_permissions(self, tenant_id: str, subject: str, allow_suspended: bool = False) -> tuple[str, ...]:
        tenant = self._tenants.get(tenant_id)
        if tenant is None:
            raise ResourceNotFound("unknown_tenant", "tenant does not exist")
        if tenant.status is TenantStatus.SUSPENDED and not allow_suspended:
            raise AuthorizationDenied("tenant_suspended", "suspended tenants cannot perform administration")
        membership = self._memberships.get(tenant_id, subject)
        if membership is None:
            raise AuthorizationDenied("unknown_subject", "subject has no tenant membership")
        if not self._subjects.exists(subject):
            raise AuthorizationDenied("deprovisioned_subject", "subject is no longer active in the identity directory")
        if membership.status is not MembershipStatus.ACTIVE:
            raise AuthorizationDenied("membership_inactive", "subject membership is not active")
        permissions: set[str] = set()
        for role_id in membership.role_ids:
            role = self._roles.get(role_id)
            if role is None:
                raise AuthorizationDenied("unknown_role", "membership references an unknown role")
            permissions.update(role.permission_ids)
        return tuple(sorted(permissions))

    def require(self, identity: VerifiedSubjectIdentity, tenant_id: str, permission: Permission, allow_suspended: bool = False) -> None:
        tenant = self._tenants.get(tenant_id)
        if tenant is None:
            raise ResourceNotFound("unknown_tenant", "tenant does not exist")
        if tenant.status is TenantStatus.SUSPENDED and not allow_suspended:
            raise AuthorizationDenied("tenant_suspended", "suspended tenants cannot perform administration")
        if self.is_platform_admin(identity):
            self.require_platform_admin(identity)
            return
        if identity.tenant_id != tenant_id:
            raise AuthorizationDenied("tenant_isolation_violation", "administrator cannot access another tenant")
        permissions = self.effective_permissions(tenant_id, identity.subject, allow_suspended)
        if permission.value not in permissions:
            raise AuthorizationDenied("permission_denied", "administrator lacks the required permission")
