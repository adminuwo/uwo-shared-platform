"""Provider credential lookup abstraction."""

from __future__ import annotations

import os
from typing import Mapping, Protocol


class SecretError(RuntimeError):
    pass


class SecretManager(Protocol):
    def get_secret(self, reference: str) -> str: ...


class EnvironmentSecretManager:
    """Resolve explicit env:// references; secret values never enter repository config."""

    def __init__(self, environment: Mapping[str, str] = os.environ) -> None:
        self._environment = environment

    def get_secret(self, reference: str) -> str:
        prefix = "env://"
        if not reference.startswith(prefix):
            raise SecretError("unsupported secret reference")
        name = reference[len(prefix):]
        value = self._environment.get(name)
        if not value:
            raise SecretError(f"required secret {name!r} is unavailable")
        return value


class MappingSecretManager:
    """In-memory secret manager for tests only."""

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = values

    def get_secret(self, reference: str) -> str:
        try:
            return self._values[reference]
        except KeyError as exc:
            raise SecretError("secret reference is unavailable") from exc
