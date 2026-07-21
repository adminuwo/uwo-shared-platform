"""Thread-safe rollback-capable billing repositories for tests only."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from packages.contracts import BillingAccount, CreditBalance, CreditReservation, LedgerEntry, RateCard, UsageEvent

from .errors import Conflict, RepositoryIntegrityError, ResourceNotFound
from .repositories import IdempotencyRecord, IdempotencyScope, LedgerPage


class FailureInjector:
    def __init__(self) -> None:
        self._point: str | None = None

    def fail_next(self, point: str) -> None:
        self._point = point

    def trigger(self, point: str) -> None:
        if self._point == point:
            self._point = None
            raise RepositoryIntegrityError("injected billing repository failure")


class InMemoryBillingAccountRepository:
    def __init__(self, failures: FailureInjector | None = None) -> None:
        self._items: dict[str, BillingAccount] = {}
        self._tenant_index: dict[str, str] = {}
        self._lock = RLock()
        self._failures = failures or FailureInjector()

    def create(self, account: BillingAccount) -> BillingAccount:
        with self._lock:
            if account.account_id in self._items or account.tenant_id in self._tenant_index:
                raise Conflict("billing_account_exists", "tenant billing account already exists")
            self._items[account.account_id] = account
            self._tenant_index[account.tenant_id] = account.account_id
            self._failures.trigger("account_write")
            return account

    def get_by_tenant(self, tenant_id: str) -> BillingAccount | None:
        with self._lock:
            account_id = self._tenant_index.get(tenant_id)
            return self._items.get(account_id) if account_id else None

    def update(self, account: BillingAccount, expected_version: int) -> BillingAccount:
        with self._lock:
            current = self._items.get(account.account_id)
            if current is None:
                raise ResourceNotFound("unknown_billing_account", "billing account does not exist")
            if current.version != expected_version:
                raise Conflict("stale_version", "billing account version is stale")
            self._items[account.account_id] = account
            self._failures.trigger("account_write")
            return account

    def _snapshot(self):
        return (dict(self._items), dict(self._tenant_index))

    def _restore(self, snapshot) -> None:
        self._items, self._tenant_index = dict(snapshot[0]), dict(snapshot[1])


class InMemoryLedgerRepository:
    def __init__(self, accounts: InMemoryBillingAccountRepository, failures: FailureInjector | None = None) -> None:
        self._accounts = accounts
        self._items: dict[str, LedgerEntry] = {}
        self._account_entries: dict[str, list[str]] = {}
        self._lock = RLock()
        self._failures = failures or FailureInjector()

    def balance(self, account_id: str, tenant_id: str, as_of: str) -> CreditBalance:
        with self._lock:
            account = self._accounts.get_by_tenant(tenant_id)
            if account is None or account.account_id != account_id:
                raise RepositoryIntegrityError("ledger account and tenant do not match a billing account")
            available = 0
            reserved = 0
            entries = self._account_entries.get(account_id, [])
            version = 1
            for entry_id in entries:
                entry = self._items[entry_id]
                try:
                    entry = replace(entry)
                except (TypeError, ValueError) as exc:
                    raise RepositoryIntegrityError("stored ledger entry violates canonical financial semantics") from exc
                if entry.account_id != account_id or entry.tenant_id != tenant_id:
                    raise RepositoryIntegrityError("stored ledger entry crosses an account or tenant boundary")
                if entry.version != version + 1:
                    raise RepositoryIntegrityError("stored ledger entry versions are not sequential")
                available += entry.available_delta_microunits
                reserved += entry.reserved_delta_microunits
                version = entry.version
            if available < 0 or reserved < 0:
                raise RepositoryIntegrityError("ledger derived a negative balance")
            return CreditBalance(account_id, tenant_id, available, reserved, version, as_of)

    def append(self, entry: LedgerEntry, expected_version: int) -> CreditBalance:
        with self._lock:
            try:
                entry = replace(entry)
            except (TypeError, ValueError) as exc:
                raise RepositoryIntegrityError("ledger entry violates canonical financial semantics") from exc
            account = self._accounts.get_by_tenant(entry.tenant_id)
            if account is None or account.account_id != entry.account_id:
                raise RepositoryIntegrityError("ledger entry account and tenant do not match")
            if entry.entry_id in self._items:
                raise Conflict("ledger_entry_exists", "ledger entry already exists")
            current = self.balance(entry.account_id, entry.tenant_id, entry.created_at)
            if current.version != expected_version:
                raise Conflict("stale_version", "credit balance version is stale")
            if entry.version != current.version + 1:
                raise Conflict("invalid_ledger_version", "ledger entry version must be sequential")
            available = current.available_microunits + entry.available_delta_microunits
            reserved = current.reserved_microunits + entry.reserved_delta_microunits
            if available < 0:
                raise Conflict("insufficient_balance", "available credit balance is insufficient")
            if reserved < 0:
                raise Conflict("invalid_reserved_balance", "reserved credit balance cannot be negative")
            self._items[entry.entry_id] = entry
            self._account_entries.setdefault(entry.account_id, []).append(entry.entry_id)
            self._failures.trigger("ledger_write")
            return CreditBalance(entry.account_id, entry.tenant_id, available, reserved, entry.version, entry.created_at)

    def get(self, entry_id: str) -> LedgerEntry | None:
        with self._lock:
            return self._items.get(entry_id)

    def list(self, account_id: str, limit: int, cursor: str | None) -> LedgerPage:
        with self._lock:
            ids = self._account_entries.get(account_id, [])
            if cursor is not None and cursor not in ids:
                raise Conflict("invalid_cursor", "ledger cursor is invalid")
            start = ids.index(cursor) + 1 if cursor is not None else 0
            selected = ids[start:start + limit]
            next_cursor = selected[-1] if selected and start + limit < len(ids) else None
            return LedgerPage(tuple(self._items[item] for item in selected), next_cursor)

    def _snapshot(self):
        return (dict(self._items), {key: list(value) for key, value in self._account_entries.items()})

    def _restore(self, snapshot) -> None:
        self._items = dict(snapshot[0])
        self._account_entries = {key: list(value) for key, value in snapshot[1].items()}


class InMemoryReservationRepository:
    def __init__(self, failures: FailureInjector | None = None) -> None:
        self._items: dict[str, CreditReservation] = {}
        self._lock = RLock()
        self._failures = failures or FailureInjector()

    def create(self, reservation: CreditReservation) -> CreditReservation:
        with self._lock:
            if reservation.reservation_id in self._items:
                raise Conflict("reservation_exists", "credit reservation already exists")
            self._items[reservation.reservation_id] = reservation
            self._failures.trigger("reservation_write")
            return reservation

    def get(self, reservation_id: str) -> CreditReservation | None:
        with self._lock:
            return self._items.get(reservation_id)

    def update(self, reservation: CreditReservation, expected_version: int) -> CreditReservation:
        with self._lock:
            current = self._items.get(reservation.reservation_id)
            if current is None:
                raise ResourceNotFound("unknown_reservation", "credit reservation does not exist")
            if current.version != expected_version:
                raise Conflict("stale_version", "reservation version is stale")
            self._items[reservation.reservation_id] = reservation
            self._failures.trigger("reservation_write")
            return reservation

    def _snapshot(self):
        return dict(self._items)

    def _restore(self, snapshot) -> None:
        self._items = dict(snapshot)


class InMemoryUsageEventRepository:
    def __init__(self, failures: FailureInjector | None = None) -> None:
        self._items: dict[str, UsageEvent] = {}
        self._lock = RLock()
        self._failures = failures or FailureInjector()

    def append(self, event: UsageEvent) -> UsageEvent:
        with self._lock:
            if event.usage_event_id in self._items:
                raise Conflict("usage_event_exists", "usage event already exists")
            self._items[event.usage_event_id] = event
            self._failures.trigger("usage_write")
            return event

    def get(self, usage_event_id: str) -> UsageEvent | None:
        with self._lock:
            return self._items.get(usage_event_id)

    def _snapshot(self):
        return dict(self._items)

    def _restore(self, snapshot) -> None:
        self._items = dict(snapshot)


class InMemoryRateCardRepository:
    def __init__(self, cards: tuple[RateCard, ...] = ()) -> None:
        self._items: dict[tuple[str, int], RateCard] = {}
        self._lock = RLock()
        for card in cards:
            self.add(card)

    def add(self, rate_card: RateCard) -> RateCard:
        with self._lock:
            key = (rate_card.rate_card_id, rate_card.version)
            if key in self._items:
                raise Conflict("rate_card_exists", "immutable rate-card version already exists")
            if any(item.rate_card_id == rate_card.rate_card_id and item.effective_at == rate_card.effective_at for item in self._items.values()):
                raise Conflict("ambiguous_rate_card", "a rate-card family cannot have multiple versions at the same effective time")
            self._items[key] = rate_card
            return rate_card

    def get(self, rate_card_id: str, version: int) -> RateCard | None:
        with self._lock:
            return self._items.get((rate_card_id, version))

    def active_at(self, as_of_utc: str) -> RateCard:
        with self._lock:
            try:
                as_of = datetime.fromisoformat(as_of_utc.replace("Z", "+00:00"))
                if as_of.utcoffset() != timezone.utc.utcoffset(as_of):
                    raise ValueError("as-of time is not UTC")
                eligible = [
                    card for card in self._items.values()
                    if datetime.fromisoformat(card.effective_at.replace("Z", "+00:00")) <= as_of
                ]
            except (AttributeError, TypeError, ValueError) as exc:
                raise RepositoryIntegrityError("rate-card lookup requires a valid UTC timestamp") from exc
            if not eligible:
                raise ResourceNotFound("unknown_rate_card", "no rate card is effective at the requested time")
            return max(eligible, key=lambda card: (datetime.fromisoformat(card.effective_at.replace("Z", "+00:00")), card.rate_card_id, card.version))

    def _snapshot(self):
        return dict(self._items)

    def _restore(self, snapshot) -> None:
        self._items = dict(snapshot)


class InMemoryIdempotencyRepository:
    def __init__(self, failures: FailureInjector | None = None) -> None:
        self._items: dict[tuple[str, str, str, str], IdempotencyRecord] = {}
        self._lock = RLock()
        self._failures = failures or FailureInjector()

    @staticmethod
    def _key(scope: IdempotencyScope, key: str) -> tuple[str, str, str, str]:
        return (scope.operation, scope.tenant_id, scope.actor_subject, key)

    def get(self, scope: IdempotencyScope, key: str) -> IdempotencyRecord | None:
        with self._lock:
            return self._items.get(self._key(scope, key))

    def put(self, record: IdempotencyRecord) -> IdempotencyRecord:
        with self._lock:
            key = self._key(record.scope, record.key)
            if key in self._items:
                raise Conflict("idempotency_conflict", "idempotency record already exists")
            self._items[key] = record
            self._failures.trigger("idempotency_write")
            return record

    def _snapshot(self):
        return dict(self._items)

    def _restore(self, snapshot) -> None:
        self._items = dict(snapshot)


class InMemoryBillingUnitOfWork:
    def __init__(self, accounts, ledger, reservations, usage, rate_cards, idempotency, transaction_lock: RLock) -> None:
        self.accounts = accounts
        self.ledger = ledger
        self.reservations = reservations
        self.usage = usage
        self.rate_cards = rate_cards
        self.idempotency = idempotency
        self._repositories = (accounts, ledger, reservations, usage, rate_cards, idempotency)
        self._transaction_lock = transaction_lock
        self._snapshots: tuple[Any, ...] | None = None
        self._committed = False

    def __enter__(self):
        self._transaction_lock.acquire()
        for repository in self._repositories:
            repository._lock.acquire()
        self._snapshots = tuple(repository._snapshot() for repository in self._repositories)
        return self

    def commit(self) -> None:
        self._committed = True

    def rollback(self) -> None:
        if self._snapshots is not None:
            for repository, snapshot in zip(self._repositories, self._snapshots):
                repository._restore(snapshot)
            self._snapshots = None

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        try:
            if exc_type is not None or not self._committed:
                self.rollback()
        finally:
            for repository in reversed(self._repositories):
                repository._lock.release()
            self._transaction_lock.release()


class InMemoryBillingUnitOfWorkFactory:
    def __init__(self, accounts, ledger, reservations, usage, rate_cards, idempotency) -> None:
        self._repositories = (accounts, ledger, reservations, usage, rate_cards, idempotency)
        self._transaction_lock = RLock()

    def __call__(self) -> InMemoryBillingUnitOfWork:
        return InMemoryBillingUnitOfWork(*self._repositories, self._transaction_lock)
