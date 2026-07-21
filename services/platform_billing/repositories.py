"""Provider-neutral billing persistence and transaction protocols."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Union

from packages.contracts import (
    BillingAccount,
    ChargeCalculation,
    CreditBalance,
    CreditReservation,
    LedgerEntry,
    RateCard,
    UsageEvent,
)


@dataclass(frozen=True)
class LedgerPage:
    items: tuple[LedgerEntry, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class AccountMutationResult:
    account: BillingAccount
    created: bool


@dataclass(frozen=True)
class CreditMutationResult:
    balance: CreditBalance
    ledger_entry: LedgerEntry
    created: bool


@dataclass(frozen=True)
class ReservationMutationResult:
    reservation: CreditReservation
    balance: CreditBalance
    ledger_entry: LedgerEntry
    created: bool


@dataclass(frozen=True)
class CaptureMutationResult:
    reservation: CreditReservation
    balance: CreditBalance
    usage_event: UsageEvent
    charge: ChargeCalculation
    ledger_entry: LedgerEntry
    created: bool


@dataclass(frozen=True)
class ReleaseMutationResult:
    reservation: CreditReservation
    balance: CreditBalance
    ledger_entry: LedgerEntry
    created: bool


@dataclass(frozen=True)
class IdempotencyScope:
    operation: str
    tenant_id: str
    actor_subject: str

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value for value in (self.operation, self.tenant_id, self.actor_subject)):
            raise ValueError("idempotency scope fields must be non-empty")


IdempotencyResult = Union[BillingAccount, CreditMutationResult, ReservationMutationResult, CaptureMutationResult, ReleaseMutationResult]


@dataclass(frozen=True)
class IdempotencyRecord:
    scope: IdempotencyScope
    key: str
    request_fingerprint: str
    original_result: IdempotencyResult


class BillingAccountRepository(Protocol):
    def create(self, account: BillingAccount) -> BillingAccount: ...
    def get_by_tenant(self, tenant_id: str) -> BillingAccount | None: ...
    def update(self, account: BillingAccount, expected_version: int) -> BillingAccount: ...


class LedgerRepository(Protocol):
    def append(self, entry: LedgerEntry, expected_version: int) -> CreditBalance: ...
    def balance(self, account_id: str, tenant_id: str, as_of: str) -> CreditBalance: ...
    def list(self, account_id: str, limit: int, cursor: str | None) -> LedgerPage: ...
    def get(self, entry_id: str) -> LedgerEntry | None: ...


class ReservationRepository(Protocol):
    def create(self, reservation: CreditReservation) -> CreditReservation: ...
    def get(self, reservation_id: str) -> CreditReservation | None: ...
    def update(self, reservation: CreditReservation, expected_version: int) -> CreditReservation: ...


class UsageEventRepository(Protocol):
    def append(self, event: UsageEvent) -> UsageEvent: ...
    def get(self, usage_event_id: str) -> UsageEvent | None: ...


class RateCardRepository(Protocol):
    def add(self, rate_card: RateCard) -> RateCard: ...
    def get(self, rate_card_id: str, version: int) -> RateCard | None: ...
    def active_at(self, as_of_utc: str) -> RateCard: ...


class IdempotencyRepository(Protocol):
    def get(self, scope: IdempotencyScope, key: str) -> IdempotencyRecord | None: ...
    def put(self, record: IdempotencyRecord) -> IdempotencyRecord: ...


class BillingUnitOfWork(Protocol):
    accounts: BillingAccountRepository
    ledger: LedgerRepository
    reservations: ReservationRepository
    usage: UsageEventRepository
    rate_cards: RateCardRepository
    idempotency: IdempotencyRepository

    def __enter__(self) -> "BillingUnitOfWork": ...
    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> BillingUnitOfWork: ...
