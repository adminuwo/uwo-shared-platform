"""Canonical schema-versioned billing, credit, usage, and rate-card contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Optional, Tuple

from .catalog import Product
from .domain import SCHEMA_VERSION, utc_now

CREDIT_MICROUNITS_PER_CREDIT = 1_000_000
TOKEN_RATE_UNIT = 1_000
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}$")


def _identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must be a stable identifier")


def _version(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("version must be a positive integer")


def _integer(value: int, name: str, *, minimum: int = 0) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")


def _signed_integer(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")


def _timestamp(value: str, name: str) -> None:
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an ISO-8601 UTC timestamp") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{name} must be an ISO-8601 UTC timestamp")


def _schema(value: str) -> None:
    if value != SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {SCHEMA_VERSION!r}")


class BillingAccountStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CLOSED = "closed"


class ReservationStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    PARTIALLY_CAPTURED = "partially_captured"
    CAPTURED = "captured"
    RELEASED = "released"


class LedgerEntryType(str, Enum):
    CREDIT_GRANT = "credit_grant"
    CREDIT_ADJUSTMENT = "credit_adjustment"
    USAGE_RESERVATION = "usage_reservation"
    USAGE_CAPTURE = "usage_capture"
    RESERVATION_RELEASE = "reservation_release"
    REFUND = "refund"


@dataclass(frozen=True)
class BillingAccount:
    account_id: str
    tenant_id: str
    status: BillingAccountStatus
    created_at: str
    updated_at: str
    version: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _identifier(self.account_id, "account_id")
        _identifier(self.tenant_id, "tenant_id")
        if not isinstance(self.status, BillingAccountStatus):
            raise ValueError("status must be a BillingAccountStatus")
        _timestamp(self.created_at, "created_at")
        _timestamp(self.updated_at, "updated_at")
        _version(self.version)


@dataclass(frozen=True)
class CreditBalance:
    account_id: str
    tenant_id: str
    available_microunits: int
    reserved_microunits: int
    version: int
    as_of: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _identifier(self.account_id, "account_id")
        _identifier(self.tenant_id, "tenant_id")
        _integer(self.available_microunits, "available_microunits")
        _integer(self.reserved_microunits, "reserved_microunits")
        _version(self.version)
        _timestamp(self.as_of, "as_of")


@dataclass(frozen=True)
class CreditReservation:
    reservation_id: str
    account_id: str
    tenant_id: str
    product: Product
    model: str
    request_id: str
    estimated_microunits: int
    captured_microunits: int
    released_microunits: int
    status: ReservationStatus
    created_at: str
    expires_at: str
    updated_at: str
    version: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        for name in ("reservation_id", "account_id", "tenant_id", "model", "request_id"):
            _identifier(getattr(self, name), name)
        if not isinstance(self.product, Product):
            raise ValueError("product must be a canonical Product")
        if not isinstance(self.status, ReservationStatus):
            raise ValueError("status must be a ReservationStatus")
        _integer(self.estimated_microunits, "estimated_microunits", minimum=1)
        _integer(self.captured_microunits, "captured_microunits")
        _integer(self.released_microunits, "released_microunits")
        if self.captured_microunits + self.released_microunits > self.estimated_microunits:
            raise ValueError("captured plus released credit cannot exceed the reservation")
        for name in ("created_at", "expires_at", "updated_at"):
            _timestamp(getattr(self, name), name)
        _version(self.version)


@dataclass(frozen=True)
class UsageDimensions:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        for name in ("input_tokens", "output_tokens", "total_tokens"):
            _integer(getattr(self, name), name)
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must equal input_tokens plus output_tokens")


@dataclass(frozen=True)
class UsageEvent:
    usage_event_id: str
    reservation_id: str
    tenant_id: str
    product: Product
    model: str
    provider_id: str
    provider_model_id: Optional[str]
    region: str
    request_id: str
    provider_request_id: Optional[str]
    dimensions: UsageDimensions
    occurred_at: str
    rate_card_id: str
    rate_card_version: int
    calculated_charge_microunits: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        for name in ("usage_event_id", "reservation_id", "tenant_id", "model", "provider_id", "region", "request_id", "rate_card_id"):
            _identifier(getattr(self, name), name)
        for name in ("provider_model_id", "provider_request_id"):
            value = getattr(self, name)
            if value is not None:
                _identifier(value, name)
        if not isinstance(self.product, Product) or not isinstance(self.dimensions, UsageDimensions):
            raise ValueError("usage event product and dimensions are invalid")
        _timestamp(self.occurred_at, "occurred_at")
        _version(self.rate_card_version)
        _integer(self.calculated_charge_microunits, "calculated_charge_microunits")


@dataclass(frozen=True)
class LedgerEntry:
    entry_id: str
    account_id: str
    tenant_id: str
    entry_type: LedgerEntryType
    amount_microunits: int
    available_delta_microunits: int
    reserved_delta_microunits: int
    reference_id: str
    created_at: str
    created_by: str
    version: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        for name in ("entry_id", "account_id", "tenant_id", "reference_id", "created_by"):
            _identifier(getattr(self, name), name)
        if not isinstance(self.entry_type, LedgerEntryType):
            raise ValueError("entry_type must be a LedgerEntryType")
        _integer(self.amount_microunits, "amount_microunits", minimum=1)
        _signed_integer(self.available_delta_microunits, "available_delta_microunits")
        _signed_integer(self.reserved_delta_microunits, "reserved_delta_microunits")
        _timestamp(self.created_at, "created_at")
        _version(self.version)
        self.validate_financial_semantics()

    def validate_financial_semantics(self) -> None:
        """Enforce the canonical accounting meaning of every entry type."""

        expected = {
            LedgerEntryType.CREDIT_GRANT: (self.amount_microunits, 0),
            LedgerEntryType.USAGE_RESERVATION: (-self.amount_microunits, self.amount_microunits),
            LedgerEntryType.USAGE_CAPTURE: (0, -self.amount_microunits),
            LedgerEntryType.RESERVATION_RELEASE: (self.amount_microunits, -self.amount_microunits),
            LedgerEntryType.REFUND: (self.amount_microunits, 0),
        }
        if self.entry_type is LedgerEntryType.CREDIT_ADJUSTMENT:
            if self.reserved_delta_microunits != 0:
                raise ValueError("credit adjustments cannot change reserved credit")
            if self.available_delta_microunits == 0 or abs(self.available_delta_microunits) != self.amount_microunits:
                raise ValueError("credit adjustment amount must equal the absolute available delta")
            return
        if (self.available_delta_microunits, self.reserved_delta_microunits) != expected[self.entry_type]:
            raise ValueError(f"{self.entry_type.value} ledger deltas do not match canonical semantics")


@dataclass(frozen=True)
class RateCardVersion:
    rate_card_id: str
    version: int
    effective_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _identifier(self.rate_card_id, "rate_card_id")
        _version(self.version)
        _timestamp(self.effective_at, "effective_at")


@dataclass(frozen=True)
class ModelPrice:
    product: Product
    model: str
    provider_id: str
    region: str
    input_token_rate_microunits: int
    output_token_rate_microunits: int
    fixed_request_microunits: int = 0
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        if not isinstance(self.product, Product):
            raise ValueError("product must be a canonical Product")
        for name in ("model", "provider_id", "region"):
            _identifier(getattr(self, name), name)
        for name in ("input_token_rate_microunits", "output_token_rate_microunits", "fixed_request_microunits"):
            _integer(getattr(self, name), name)

    @property
    def key(self) -> Tuple[str, str, str, str]:
        return (self.product.value, self.model, self.provider_id, self.region)


@dataclass(frozen=True)
class RateCard:
    rate_card_id: str
    version: int
    effective_at: str
    prices: Tuple[ModelPrice, ...]
    created_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _identifier(self.rate_card_id, "rate_card_id")
        _version(self.version)
        _timestamp(self.effective_at, "effective_at")
        _timestamp(self.created_at, "created_at")
        if not self.prices or not all(isinstance(item, ModelPrice) for item in self.prices):
            raise ValueError("prices must contain at least one ModelPrice")
        keys = tuple(item.key for item in self.prices)
        if len(set(keys)) != len(keys) or keys != tuple(sorted(keys)):
            raise ValueError("rate-card prices must be unique and deterministically sorted")

    @property
    def identity(self) -> RateCardVersion:
        return RateCardVersion(self.rate_card_id, self.version, self.effective_at)


@dataclass(frozen=True)
class ChargeCalculation:
    rate_card_id: str
    rate_card_version: int
    input_charge_microunits: int
    output_charge_microunits: int
    fixed_charge_microunits: int
    total_charge_microunits: int
    calculated_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _identifier(self.rate_card_id, "rate_card_id")
        _version(self.rate_card_version)
        values = (self.input_charge_microunits, self.output_charge_microunits, self.fixed_charge_microunits)
        for name, value in zip(("input_charge_microunits", "output_charge_microunits", "fixed_charge_microunits"), values):
            _integer(value, name)
        if self.total_charge_microunits != sum(values):
            raise ValueError("total charge must equal its components")
        _timestamp(self.calculated_at, "calculated_at")


@dataclass(frozen=True)
class BillingDecision:
    decision_id: str
    tenant_id: str
    approved: bool
    estimated_charge_microunits: int
    reason_code: str
    decided_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        for name in ("decision_id", "tenant_id", "reason_code"):
            _identifier(getattr(self, name), name)
        if not isinstance(self.approved, bool):
            raise ValueError("approved must be boolean")
        _integer(self.estimated_charge_microunits, "estimated_charge_microunits")
        _timestamp(self.decided_at, "decided_at")


__all__ = [
    "BillingAccount", "BillingAccountStatus", "BillingDecision", "ChargeCalculation",
    "CREDIT_MICROUNITS_PER_CREDIT", "CreditBalance", "CreditReservation", "LedgerEntry",
    "LedgerEntryType", "ModelPrice", "RateCard", "RateCardVersion", "ReservationStatus",
    "TOKEN_RATE_UNIT", "UsageDimensions", "UsageEvent", "utc_now",
]
