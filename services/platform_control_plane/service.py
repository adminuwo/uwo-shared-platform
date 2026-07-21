"""Application service for identity, tenancy, roles, and entitlements."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Callable, Type, TypeVar

from packages.contracts import (
    MembershipStatus,
    ModelEntitlement,
    Permission,
    PolicyDocument,
    Product,
    ProductEntitlement,
    Role,
    Tenant,
    TenantMembership,
    TenantStatus,
    VerifiedSubjectIdentity,
    utc_now,
)

from .audit import AuditSink, audit_event
from .authorization import ControlPlaneAuthorizer, SubjectDirectory
from .errors import AuthorizationDenied, Conflict, InvalidRequest, RepositoryIntegrityError, ResourceNotFound
from .repositories import (
    CreateResult,
    EntitlementMutationResult,
    EntitlementRepository,
    EntitlementSnapshot,
    IdempotencyRecord,
    IdempotencyScope,
    MembershipRepository,
    Page,
    PolicyVersionRepository,
    RoleRepository,
    TenantRepository,
    UnitOfWorkFactory,
)

TENANT_ADMIN_ROLE = "tenant-admin"
TENANT_READER_ROLE = "tenant-reader"
T = TypeVar("T", Tenant, ProductEntitlement, ModelEntitlement)


def built_in_roles(timestamp: str | None = None) -> tuple[Role, ...]:
    created_at = timestamp or utc_now()
    admin_permissions = tuple(sorted(item.value for item in Permission))
    reader_permissions = tuple(sorted((Permission.TENANT_READ.value, Permission.ENTITLEMENT_READ.value, Permission.POLICY_READ.value)))
    return (
        Role(TENANT_ADMIN_ROLE, "Tenant administrator", admin_permissions, created_at, 1),
        Role(TENANT_READER_ROLE, "Tenant reader", reader_permissions, created_at, 1),
    )


@dataclass(frozen=True)
class MembershipMutationResult:
    membership: TenantMembership
    created: bool


def _fingerprint(value: dict[str, object]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(payload).hexdigest()


def _require_idempotency_key(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise InvalidRequest("invalid_idempotency_key", "idempotency key must contain 1 to 128 characters")


def _contract(factory: Callable[[], T]) -> T:
    try:
        return factory()
    except (TypeError, ValueError) as exc:
        raise InvalidRequest("invalid_request", str(exc)) from exc


def _replay(record: IdempotencyRecord, fingerprint: str, expected_type: Type[T]) -> T:
    if record.request_fingerprint != fingerprint:
        raise Conflict("idempotency_conflict", "idempotency key was already used with different request input")
    if not isinstance(record.original_result, expected_type):
        raise RepositoryIntegrityError("idempotency ledger result type does not match its operation")
    return record.original_result


class PlatformControlPlane:
    def __init__(
        self,
        tenants: TenantRepository,
        memberships: MembershipRepository,
        roles: RoleRepository,
        entitlements: EntitlementRepository,
        policies: PolicyVersionRepository,
        unit_of_work: UnitOfWorkFactory,
        subjects: SubjectDirectory,
        authorizer: ControlPlaneAuthorizer,
        audit: AuditSink,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self._tenants = tenants
        self._memberships = memberships
        self._roles = roles
        self._entitlements = entitlements
        self._policies = policies
        self._unit_of_work = unit_of_work
        self._subjects = subjects
        self._authorizer = authorizer
        self._audit = audit
        self._clock = clock

    def _require(self, identity: VerifiedSubjectIdentity, tenant_id: str, permission: Permission, allow_suspended: bool = False) -> None:
        self._authorizer.require(identity, tenant_id, permission, allow_suspended)

    def _platform_only(self, identity: VerifiedSubjectIdentity) -> None:
        self._authorizer.require_platform_admin(identity)

    @staticmethod
    def _scope(operation: str, tenant_id: str, identity: VerifiedSubjectIdentity) -> IdempotencyScope:
        return IdempotencyScope(operation, tenant_id, identity.subject)

    def create_tenant(self, identity: VerifiedSubjectIdentity, tenant_id: str, name: str, region: str, idempotency_key: str, request_id: str) -> CreateResult:
        self._platform_only(identity)
        _require_idempotency_key(idempotency_key)
        timestamp = self._clock()
        tenant = _contract(lambda: Tenant(tenant_id, name, TenantStatus.ACTIVE, region, timestamp, timestamp, 1))
        policy = _contract(lambda: PolicyDocument(
            tenant_id,
            1,
            {"metadata": {"document_type": "tenant-policy", "schema_version": "1"}},
            timestamp,
            identity.subject,
        ))
        fingerprint = _fingerprint({"tenant_id": tenant_id, "name": name, "region": region})
        scope = self._scope("tenant.create", tenant_id, identity)
        try:
            with self._unit_of_work() as transaction:
                existing = transaction.idempotency.get(scope, idempotency_key)
                if existing is not None:
                    replay = _replay(existing, fingerprint, Tenant)
                    transaction.commit()
                    return CreateResult(replay, False)
                transaction.tenants.create(tenant)
                transaction.entitlements.initialize(tenant_id)
                transaction.policies.create_initial(policy)
                transaction.idempotency.put(IdempotencyRecord(scope, idempotency_key, fingerprint, replace(tenant)))
                transaction.commit()
        except Exception:
            self._audit.emit(audit_event(
                "tenant.provisioning_rolled_back",
                request_id,
                "failed",
                actor_subject=identity.subject,
                tenant_id=tenant_id,
                reason_code="transaction_rolled_back",
            ))
            raise
        self._audit.emit(audit_event("tenant.created", request_id, "succeeded", actor_subject=identity.subject, tenant_id=tenant_id))
        return CreateResult(tenant, True)

    def get_tenant(self, identity: VerifiedSubjectIdentity, tenant_id: str, request_id: str) -> Tenant:
        self._require(identity, tenant_id, Permission.TENANT_READ, allow_suspended=True)
        tenant = self._tenants.get(tenant_id)
        if tenant is None:
            raise ResourceNotFound("unknown_tenant", "tenant does not exist")
        return tenant

    def list_tenants(self, identity: VerifiedSubjectIdentity, limit: int, cursor: str | None, request_id: str) -> Page:
        self._platform_only(identity)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 100:
            raise InvalidRequest("invalid_pagination", "limit must be between 1 and 100")
        return self._tenants.list(limit, cursor)

    def set_tenant_status(self, identity: VerifiedSubjectIdentity, tenant_id: str, status: TenantStatus, expected_version: int, request_id: str) -> Tenant:
        current = self._tenants.get(tenant_id)
        if current is None:
            raise ResourceNotFound("unknown_tenant", "tenant does not exist")
        if current.status is TenantStatus.SUSPENDED:
            self._platform_only(identity)
        else:
            self._require(identity, tenant_id, Permission.TENANT_MANAGE)
        if current.status is status:
            raise Conflict("status_unchanged", "tenant already has the requested status")
        updated = replace(current, status=status, updated_at=self._clock(), version=current.version + 1)
        result = self._tenants.update(updated, expected_version)
        self._audit.emit(audit_event("tenant.status_changed", request_id, "succeeded", actor_subject=identity.subject, tenant_id=tenant_id, resource_id=status.value))
        return result

    def _target_membership(self, tenant_id: str, subject: str) -> TenantMembership:
        membership = self._memberships.get(tenant_id, subject)
        if membership is None:
            raise ResourceNotFound("unknown_subject", "subject has no tenant membership")
        if not self._subjects.exists(subject):
            raise AuthorizationDenied("deprovisioned_subject", "subject is no longer active in the identity directory")
        return membership

    def put_membership(self, identity: VerifiedSubjectIdentity, tenant_id: str, subject: str, status: MembershipStatus, expected_version: int, request_id: str) -> MembershipMutationResult:
        self._require(identity, tenant_id, Permission.MEMBERSHIP_MANAGE)
        current = self._memberships.get(tenant_id, subject)
        if not self._subjects.exists(subject):
            if current is None:
                raise ResourceNotFound("unknown_subject", "subject is not verified by the identity directory")
            raise AuthorizationDenied("deprovisioned_subject", "subject is no longer active in the identity directory")
        timestamp = self._clock()
        if current is None:
            if expected_version != 0:
                raise Conflict("stale_version", "new membership expected_version must be zero")
            membership = _contract(lambda: TenantMembership(f"membership:{tenant_id}:{subject}", tenant_id, subject, status, (), timestamp, timestamp, 1))
            result = self._memberships.create(membership)
            created = True
        else:
            if expected_version == 0:
                raise Conflict("membership_exists", "membership already exists")
            result = self._memberships.update(replace(current, status=status, updated_at=timestamp, version=current.version + 1), expected_version)
            created = False
        self._audit.emit(audit_event("membership.changed", request_id, "succeeded", actor_subject=identity.subject, tenant_id=tenant_id, target_subject=subject, resource_id=status.value))
        return MembershipMutationResult(result, created)

    def assign_role(self, identity: VerifiedSubjectIdentity, tenant_id: str, subject: str, role_id: str, expected_version: int, request_id: str) -> TenantMembership:
        self._require(identity, tenant_id, Permission.ROLE_MANAGE)
        membership = self._target_membership(tenant_id, subject)
        role = self._roles.get(role_id)
        if role is None:
            raise ResourceNotFound("unknown_role", "role does not exist")
        if role_id in membership.role_ids:
            raise Conflict("role_assignment_exists", "role is already assigned")
        updated = replace(membership, role_ids=tuple(sorted(membership.role_ids + (role_id,))), updated_at=self._clock(), version=membership.version + 1)
        result = self._memberships.update(updated, expected_version)
        self._audit.emit(audit_event("role.assigned", request_id, "succeeded", actor_subject=identity.subject, tenant_id=tenant_id, target_subject=subject, resource_id=role_id))
        return result

    def revoke_role(self, identity: VerifiedSubjectIdentity, tenant_id: str, subject: str, role_id: str, expected_version: int, request_id: str) -> TenantMembership:
        self._require(identity, tenant_id, Permission.ROLE_MANAGE)
        membership = self._target_membership(tenant_id, subject)
        if self._roles.get(role_id) is None:
            raise ResourceNotFound("unknown_role", "role does not exist")
        if role_id not in membership.role_ids:
            raise Conflict("role_assignment_missing", "role is not assigned")
        updated = replace(membership, role_ids=tuple(item for item in membership.role_ids if item != role_id), updated_at=self._clock(), version=membership.version + 1)
        result = self._memberships.update(updated, expected_version)
        self._audit.emit(audit_event("role.revoked", request_id, "succeeded", actor_subject=identity.subject, tenant_id=tenant_id, target_subject=subject, resource_id=role_id))
        return result

    def effective_permissions(self, identity: VerifiedSubjectIdentity, tenant_id: str, subject: str, request_id: str) -> tuple[str, ...]:
        self._require(identity, tenant_id, Permission.ROLE_MANAGE)
        return self._authorizer.effective_permissions(tenant_id, subject)

    def _grant(
        self,
        scope: IdempotencyScope,
        key: str,
        fingerprint: str,
        expected_type: Type[T],
        mutation: Callable[[EntitlementRepository], T],
    ) -> EntitlementMutationResult:
        with self._unit_of_work() as transaction:
            existing = transaction.idempotency.get(scope, key)
            if existing is not None:
                replay = _replay(existing, fingerprint, expected_type)
                transaction.commit()
                return EntitlementMutationResult(replay, False)
            stored = mutation(transaction.entitlements)
            transaction.idempotency.put(IdempotencyRecord(scope, key, fingerprint, replace(stored)))
            transaction.commit()
        return EntitlementMutationResult(stored, True)

    def grant_product(self, identity: VerifiedSubjectIdentity, tenant_id: str, product: Product, expected_version: int, idempotency_key: str, request_id: str) -> EntitlementMutationResult:
        self._require(identity, tenant_id, Permission.ENTITLEMENT_MANAGE)
        _require_idempotency_key(idempotency_key)
        item = _contract(lambda: ProductEntitlement(f"product:{tenant_id}:{product.value}", tenant_id, product, self._clock(), identity.subject, 1))
        fingerprint = _fingerprint({"tenant_id": tenant_id, "product": product.value, "expected_version": expected_version})
        result = self._grant(
            self._scope("entitlement.product.grant", tenant_id, identity),
            idempotency_key,
            fingerprint,
            ProductEntitlement,
            lambda repository: repository.grant_product(item, expected_version),
        )
        if result.created:
            self._audit.emit(audit_event("entitlement.product_granted", request_id, "succeeded", actor_subject=identity.subject, tenant_id=tenant_id, resource_id=product.value))
        return result

    def revoke_product(self, identity: VerifiedSubjectIdentity, tenant_id: str, product: Product, expected_version: int, request_id: str) -> EntitlementSnapshot:
        self._require(identity, tenant_id, Permission.ENTITLEMENT_MANAGE)
        result = self._entitlements.revoke_product(tenant_id, product, expected_version)
        self._audit.emit(audit_event("entitlement.product_revoked", request_id, "succeeded", actor_subject=identity.subject, tenant_id=tenant_id, resource_id=product.value))
        return result

    def grant_model(self, identity: VerifiedSubjectIdentity, tenant_id: str, model: str, expected_version: int, idempotency_key: str, request_id: str) -> EntitlementMutationResult:
        self._require(identity, tenant_id, Permission.ENTITLEMENT_MANAGE)
        _require_idempotency_key(idempotency_key)
        item = _contract(lambda: ModelEntitlement(f"model:{tenant_id}:{model}", tenant_id, model, self._clock(), identity.subject, 1))
        fingerprint = _fingerprint({"tenant_id": tenant_id, "model": model, "expected_version": expected_version})
        result = self._grant(
            self._scope("entitlement.model.grant", tenant_id, identity),
            idempotency_key,
            fingerprint,
            ModelEntitlement,
            lambda repository: repository.grant_model(item, expected_version),
        )
        if result.created:
            self._audit.emit(audit_event("entitlement.model_granted", request_id, "succeeded", actor_subject=identity.subject, tenant_id=tenant_id, resource_id=model))
        return result

    def revoke_model(self, identity: VerifiedSubjectIdentity, tenant_id: str, model: str, expected_version: int, request_id: str) -> EntitlementSnapshot:
        self._require(identity, tenant_id, Permission.ENTITLEMENT_MANAGE)
        result = self._entitlements.revoke_model(tenant_id, model, expected_version)
        self._audit.emit(audit_event("entitlement.model_revoked", request_id, "succeeded", actor_subject=identity.subject, tenant_id=tenant_id, resource_id=model))
        return result

    def effective_entitlements(self, identity: VerifiedSubjectIdentity, tenant_id: str, request_id: str) -> EntitlementSnapshot:
        self._require(identity, tenant_id, Permission.ENTITLEMENT_READ)
        return self._entitlements.snapshot(tenant_id)

    def policy_version(self, identity: VerifiedSubjectIdentity, tenant_id: str, request_id: str) -> PolicyDocument:
        self._require(identity, tenant_id, Permission.POLICY_READ, allow_suspended=True)
        document = self._policies.current(tenant_id)
        if document is None:
            raise ResourceNotFound("unknown_policy", "tenant policy does not exist")
        return document
