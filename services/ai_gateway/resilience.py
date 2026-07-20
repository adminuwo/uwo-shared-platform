"""Bounded retry and per-provider circuit-breaker execution controls."""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock
from typing import Callable, Mapping

from .providers import ProviderAdapter, ProviderError, ProviderRequest, ProviderResponse


class ProviderUnavailable(RuntimeError):
    def __init__(self, failures: tuple[str, ...]) -> None:
        super().__init__("all eligible providers failed")
        self.failures = failures


@dataclass(frozen=True)
class ResiliencePolicy:
    timeout_seconds: float = 15.0
    max_attempts: int = 2
    retry_backoff_seconds: float = 0.05
    circuit_failure_threshold: int = 3
    circuit_reset_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.max_attempts < 1 or self.retry_backoff_seconds < 0:
            raise ValueError("invalid timeout or retry policy")
        if self.circuit_failure_threshold < 1 or self.circuit_reset_seconds <= 0:
            raise ValueError("invalid circuit-breaker policy")


@dataclass
class _CircuitState:
    failures: int = 0
    opened_at: float | None = None


@dataclass(frozen=True)
class ExecutionResult:
    provider_id: str
    response: ProviderResponse


class ResilientProviderExecutor:
    def __init__(self, adapters: Mapping[str, ProviderAdapter], policy: ResiliencePolicy = ResiliencePolicy(), clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        self._adapters = dict(adapters)
        self._policy = policy
        self._clock = clock
        self._sleep = sleep
        self._states = {provider_id: _CircuitState() for provider_id in adapters}
        self._lock = Lock()

    def _available(self, provider_id: str) -> bool:
        with self._lock:
            state = self._states[provider_id]
            if state.opened_at is None:
                return True
            if self._clock() - state.opened_at >= self._policy.circuit_reset_seconds:
                state.failures = 0
                state.opened_at = None
                return True
            return False

    def _success(self, provider_id: str) -> None:
        with self._lock:
            self._states[provider_id] = _CircuitState()

    def _failure(self, provider_id: str) -> None:
        with self._lock:
            state = self._states[provider_id]
            state.failures += 1
            if state.failures >= self._policy.circuit_failure_threshold:
                state.opened_at = self._clock()

    def execute(self, provider_ids: tuple[str, ...], request: ProviderRequest) -> ExecutionResult:
        failures: list[str] = []
        for provider_id in provider_ids:
            adapter = self._adapters.get(provider_id)
            if adapter is None:
                failures.append(f"{provider_id}:adapter_unavailable")
                continue
            if not self._available(provider_id):
                failures.append(f"{provider_id}:circuit_open")
                continue
            for attempt in range(1, self._policy.max_attempts + 1):
                try:
                    response = adapter.execute(request, self._policy.timeout_seconds)
                except ProviderError as exc:
                    failures.append(f"{provider_id}:provider_error")
                    if not exc.fallback_allowed:
                        raise
                    if exc.retryable:
                        self._failure(provider_id)
                    if not exc.retryable or attempt == self._policy.max_attempts or not self._available(provider_id):
                        break
                    self._sleep(self._policy.retry_backoff_seconds * attempt)
                else:
                    self._success(provider_id)
                    return ExecutionResult(provider_id, response)
        raise ProviderUnavailable(tuple(failures))
