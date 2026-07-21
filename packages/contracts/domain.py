"""Versioned identity, tenancy, role, and entitlement domain contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any, Mapping

from .catalog import Product

SCHEMA_VERSION = "1"
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}$")


class _FrozenJsonArray(tuple):
    """Internal immutable representation of an accepted JSON array."""


def utc_now() -> str:
    """Return an explicit UTC ISO-8601 timestamp."""

    return datetime.now(timezone.utc).isoformat()


def _require_identifier(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field_name} must be a stable identifier of at most 256 safe characters")


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(f"{field_name} must be non-empty text of at most 256 characters")


def _require_utc(value: str, field_name: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 UTC timestamp") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{field_name} must be an ISO-8601 UTC timestamp")


def _validate_contract(schema_version: str, identifiers: Mapping[str, str], timestamps: Mapping[str, str], version: int | None = None) -> None:
    if schema_version != SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {SCHEMA_VERSION!r}")
    for name, value in identifiers.items():
        _require_identifier(value, name)
    for name, value in timestamps.items():
        _require_utc(value, name)
    if version is not None and (not isinstance(version, int) or isinstance(version, bool) or version < 1):
        raise ValueError("version must be a positive integer")


def freeze_json(value: Any, path: str = "$") -> Any:
    """Validate and deeply freeze a JSON-compatible value deterministically."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError(f"{path} contains a non-string object key")
        return MappingProxyType({key: freeze_json(value[key], f"{path}.{key}") for key in sorted(value)})
    if isinstance(value, _FrozenJsonArray):
        return value
    if isinstance(value, list):
        return _FrozenJsonArray(freeze_json(item, f"{path}[{index}]") for index, item in enumerate(value))
    raise ValueError(f"{path} contains an unsupported JSON value")


def thaw_json(value: Any) -> Any:
    """Convert a frozen JSON value into ordinary serialization primitives."""

    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    """Serialize a JSON value with stable ordering and no non-finite numbers."""

    frozen = freeze_json(value)
    return json.dumps(thaw_json(frozen), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


class TenantStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class MembershipStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class Permission(str, Enum):
    TENANT_READ = "tenant.read"
    TENANT_MANAGE = "tenant.manage"
    MEMBERSHIP_MANAGE = "membership.manage"
    ROLE_MANAGE = "role.manage"
    ENTITLEMENT_READ = "entitlement.read"
    ENTITLEMENT_MANAGE = "entitlement.manage"
    POLICY_READ = "policy.read"


@dataclass(frozen=True)
class Tenant:
    tenant_id: str
    name: str
    status: TenantStatus
    region: str
    created_at: str
    updated_at: str
    version: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.status, TenantStatus):
            raise ValueError("status must be a TenantStatus")
        _validate_contract(
            self.schema_version,
            {"tenant_id": self.tenant_id, "region": self.region},
            {"created_at": self.created_at, "updated_at": self.updated_at},
            self.version,
        )
        _require_text(self.name, "name")


@dataclass(frozen=True)
class VerifiedSubjectIdentity:
    subject: str
    tenant_id: str
    verified_at: str = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_contract(
            self.schema_version,
            {"subject": self.subject, "tenant_id": self.tenant_id},
            {"verified_at": self.verified_at},
        )


@dataclass(frozen=True)
class TenantMembership:
    membership_id: str
    tenant_id: str
    subject: str
    status: MembershipStatus
    role_ids: tuple[str, ...]
    created_at: str
    updated_at: str
    version: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.status, MembershipStatus):
            raise ValueError("status must be a MembershipStatus")
        _validate_contract(
            self.schema_version,
            {"membership_id": self.membership_id, "tenant_id": self.tenant_id, "subject": self.subject},
            {"created_at": self.created_at, "updated_at": self.updated_at},
            self.version,
        )
        if tuple(sorted(set(self.role_ids))) != self.role_ids:
            raise ValueError("role_ids must be unique and deterministically sorted")
        for role_id in self.role_ids:
            _require_identifier(role_id, "role_id")


@dataclass(frozen=True)
class Role:
    role_id: str
    name: str
    permission_ids: tuple[str, ...]
    created_at: str
    version: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_contract(
            self.schema_version,
            {"role_id": self.role_id},
            {"created_at": self.created_at},
            self.version,
        )
        _require_text(self.name, "name")
        known = {item.value for item in Permission}
        if tuple(sorted(set(self.permission_ids))) != self.permission_ids or not set(self.permission_ids) <= known:
            raise ValueError("permission_ids must be known, unique, and deterministically sorted")


@dataclass(frozen=True)
class ProductEntitlement:
    entitlement_id: str
    tenant_id: str
    product: Product
    granted_at: str
    granted_by: str
    version: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.product, Product):
            raise ValueError("product must be a canonical Product")
        _validate_contract(
            self.schema_version,
            {"entitlement_id": self.entitlement_id, "tenant_id": self.tenant_id, "granted_by": self.granted_by},
            {"granted_at": self.granted_at},
            self.version,
        )


@dataclass(frozen=True)
class ModelEntitlement:
    entitlement_id: str
    tenant_id: str
    model: str
    granted_at: str
    granted_by: str
    version: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_contract(
            self.schema_version,
            {"entitlement_id": self.entitlement_id, "tenant_id": self.tenant_id, "model": self.model, "granted_by": self.granted_by},
            {"granted_at": self.granted_at},
            self.version,
        )


@dataclass(frozen=True)
class PolicyDocument:
    tenant_id: str
    policy_version: int
    policy: Mapping[str, Any]
    created_at: str
    created_by: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_contract(
            self.schema_version,
            {"tenant_id": self.tenant_id, "created_by": self.created_by},
            {"created_at": self.created_at},
            self.policy_version,
        )
        if not isinstance(self.policy, Mapping):
            raise ValueError("policy must be an object")
        object.__setattr__(self, "policy", freeze_json(self.policy))

    def canonical_policy(self) -> str:
        return canonical_json(self.policy)

    def policy_fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_policy().encode("utf-8")).hexdigest()
