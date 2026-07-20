"""Injected authentication boundary for trusted internal callers."""

from typing import Protocol

from packages.contracts import VerifiedSubjectIdentity


class AuthenticationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Authenticator(Protocol):
    def authenticate(self, authorization: str) -> VerifiedSubjectIdentity: ...
