import hashlib

from packages.contracts import MembershipStatus, VerifiedSubjectIdentity
from services.platform_governance.in_memory import InMemoryGovernanceState, InMemoryGovernanceUnitOfWorkFactory
from services.platform_governance.service import PlatformGovernanceService
from services.platform_operations.in_memory import DeterministicTelemetryExporter, InMemoryOperationsState, InMemoryOperationsUnitOfWorkFactory
from services.platform_operations.service import PlatformOperationsService
from services.platform_tenant_admin.in_memory import InMemoryTenantAdministrationState, InMemoryTenantAdministrationUnitOfWorkFactory
from services.platform_tenant_admin.repositories import ExternalStepReceipt
from services.platform_tenant_admin.service import PlatformTenantAdministrationService, StepExecutionFailure

from control_plane_support import ADMIN_A, PLATFORM, USER_A
from data_services_support import make_data_context

PHASE3D_NOW = "2026-07-24T06:00:00+00:00"


class FakeAdministrationClients:
    def __init__(self):
        self.calls = []; self.results = {}; self.fail_once = None
        self.tenant_status = "active"; self.billing_status = "active"; self.release_id = "release-initial"

    def _call(self, operation, tenant_id, key):
        fingerprint = (operation, tenant_id, key)
        self.calls.append(fingerprint)
        if self.fail_once == operation:
            self.fail_once = None
            raise StepExecutionFailure("dependency_unavailable", retryable=True)
        if fingerprint not in self.results:
            digest = hashlib.sha256(repr(fingerprint).encode()).hexdigest()
            self.results[fingerprint] = ExternalStepReceipt(f"result-{digest[:16]}", digest)
        return self.results[fingerprint]

    def validate_tenant(self, tenant_id, region, idempotency_key): return self._call("validate-tenant-region", tenant_id, idempotency_key)
    def ensure_baseline_membership(self, tenant_id, metadata, idempotency_key): return self._call("baseline-membership-roles", tenant_id, idempotency_key)
    def ensure_entitlements(self, tenant_id, metadata, idempotency_key): return self._call("product-model-entitlements", tenant_id, idempotency_key)
    def set_tenant_status(self, tenant_id, status, idempotency_key):
        result = self._call(f"{status}-authoritative-tenant", tenant_id, idempotency_key); self.tenant_status = status; return result
    def ensure_account_ready(self, tenant_id, idempotency_key): return self._call("billing-account-readiness", tenant_id, idempotency_key)
    def ensure_baseline_preferences(self, tenant_id, idempotency_key): return self._call("notification-preferences", tenant_id, idempotency_key)
    def ensure_initial_policy_release(self, tenant_id, idempotency_key): return self._call("initial-policy-release", tenant_id, idempotency_key)
    def register_tenant_operations(self, tenant_id, idempotency_key): return self._call("operations-registration", tenant_id, idempotency_key)
    def tenant_profile(self, tenant_id): return {"region": "in", "status": self.tenant_status}
    def billing_profile(self, tenant_id): return {"status": self.billing_status}
    def active_release_id(self, tenant_id): return self.release_id


def phase3d_context():
    context = make_data_context()
    membership = context.control.service.put_membership(PLATFORM, "tenant-a", USER_A.subject, MembershipStatus.ACTIVE, 0, "phase3d-member").membership
    context.control.service.assign_role(PLATFORM, "tenant-a", USER_A.subject, "tenant-admin", membership.version, "phase3d-role")
    return context


def tenant_admin_fixture():
    context = phase3d_context(); state = InMemoryTenantAdministrationState(); clients = FakeAdministrationClients()
    service = PlatformTenantAdministrationService(InMemoryTenantAdministrationUnitOfWorkFactory(state), context.authorizer, context.audit, clients, clients, clients, clients, clients, clock=lambda: PHASE3D_NOW)
    return context, state, clients, service


def governance_fixture():
    context = phase3d_context(); state = InMemoryGovernanceState()
    service = PlatformGovernanceService(InMemoryGovernanceUnitOfWorkFactory(state), context.authorizer, context.audit, clock=lambda: PHASE3D_NOW)
    return context, state, service


def operations_fixture():
    context = phase3d_context(); state = InMemoryOperationsState(); exporter = DeterministicTelemetryExporter()
    service = PlatformOperationsService(InMemoryOperationsUnitOfWorkFactory(state), context.authorizer, context.audit, exporter, clock=lambda: PHASE3D_NOW)
    return context, state, exporter, service
