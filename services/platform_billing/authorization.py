"""Billing authorization composed with the Phase 3A identity boundary."""

from __future__ import annotations

from packages.contracts import Permission, TenantStatus, VerifiedSubjectIdentity
from services.platform_control_plane.authorization import ControlPlaneAuthorizer, SubjectDirectory
from services.platform_control_plane.repositories import TenantRepository

from .errors import AuthorizationDenied, ResourceNotFound


class BillingAuthorizer:
    def __init__(
        self,
        tenants: TenantRepository,
        subjects: SubjectDirectory,
        control_plane: ControlPlaneAuthorizer,
        trusted_executor_subjects: frozenset[str],
    ) -> None:
        self._tenants = tenants
        self._subjects = subjects
        self._control_plane = control_plane
        self._trusted_executor_subjects = trusted_executor_subjects

    @staticmethod
    def _translate(action) -> None:
        try:
            action()
        except Exception as exc:
            code = getattr(exc, "code", "authorization_denied")
            if code == "unknown_tenant":
                raise ResourceNotFound(code, "tenant does not exist") from exc
            raise AuthorizationDenied(code, str(exc)) from exc

    def require_platform_admin(self, identity: VerifiedSubjectIdentity) -> None:
        self._translate(lambda: self._control_plane.require_platform_admin(identity))

    def require_read(self, identity: VerifiedSubjectIdentity, tenant_id: str) -> None:
        self._translate(lambda: self._control_plane.require(identity, tenant_id, Permission.BILLING_READ, allow_suspended=True))

    def require_executor(self, identity: VerifiedSubjectIdentity, tenant_id: str, *, allow_suspended: bool = False) -> None:
        tenant = self._tenants.get(tenant_id)
        if tenant is None:
            raise ResourceNotFound("unknown_tenant", "tenant does not exist")
        if tenant.status is TenantStatus.SUSPENDED and not allow_suspended:
            raise AuthorizationDenied("tenant_suspended", "suspended tenants cannot create or capture reservations")
        if not self._subjects.exists(identity.subject):
            raise AuthorizationDenied("deprovisioned_subject", "executor is no longer active in the identity directory")
        if identity.subject not in self._trusted_executor_subjects:
            raise AuthorizationDenied("billing_executor_required", "trusted billing executor authorization is required")

    def require_active_tenant(self, tenant_id: str) -> None:
        tenant = self._tenants.get(tenant_id)
        if tenant is None:
            raise ResourceNotFound("unknown_tenant", "tenant does not exist")
        if tenant.status is TenantStatus.SUSPENDED:
            raise AuthorizationDenied("tenant_suspended", "suspended tenants cannot create or capture reservations")
