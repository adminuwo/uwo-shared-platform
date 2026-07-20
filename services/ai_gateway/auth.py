"""Authentication boundary and verified tenant identity contracts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Callable, Protocol


class AuthenticationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class VerifiedIdentity:
    subject: str
    tenant_id: str


class Authenticator(Protocol):
    def authenticate(self, authorization: str) -> VerifiedIdentity: ...


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class HmacBearerAuthenticator:
    """Verify short-lived internal bearer assertions signed at a trusted edge."""

    def __init__(self, signing_key: str, clock: Callable[[], float] = time.time, issuer: str = "uwo-edge", audience: str = "uwo-ai-gateway") -> None:
        if len(signing_key) < 32:
            raise ValueError("authentication signing key must contain at least 32 characters")
        self._key = signing_key.encode("utf-8")
        self._clock = clock
        self._issuer = issuer
        self._audience = audience

    def authenticate(self, authorization: str) -> VerifiedIdentity:
        if not authorization.startswith("Bearer "):
            raise AuthenticationError("missing_bearer_token", "a bearer token is required")
        token = authorization[7:]
        try:
            encoded_payload, encoded_signature = token.split(".", 1)
            signature = _decode(encoded_signature)
            expected = hmac.new(self._key, encoded_payload.encode("ascii"), hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise AuthenticationError("invalid_token", "bearer token signature is invalid")
            payload = json.loads(_decode(encoded_payload))
            subject = payload["sub"]
            tenant_id = payload["tenant_id"]
            expires_at = int(payload["exp"])
            issuer = payload["iss"]
            audience = payload["aud"]
        except AuthenticationError:
            raise
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise AuthenticationError("invalid_token", "bearer token is malformed") from exc
        if not isinstance(subject, str) or not subject or not isinstance(tenant_id, str) or not tenant_id:
            raise AuthenticationError("invalid_token", "bearer token identity claims are invalid")
        if len(subject) > 256 or len(tenant_id) > 256:
            raise AuthenticationError("invalid_token", "bearer token identity claims are too long")
        if issuer != self._issuer or audience != self._audience:
            raise AuthenticationError("invalid_token", "bearer token issuer or audience is invalid")
        if expires_at <= int(self._clock()):
            raise AuthenticationError("expired_token", "bearer token has expired")
        return VerifiedIdentity(subject=subject, tenant_id=tenant_id)


def issue_test_token(signing_key: str, subject: str, tenant_id: str, expires_at: int, issuer: str = "uwo-edge", audience: str = "uwo-ai-gateway") -> str:
    """Create a token for tests and local fixtures; production identity is issued upstream."""

    payload = _encode(json.dumps({"sub": subject, "tenant_id": tenant_id, "exp": expires_at, "iss": issuer, "aud": audience}, separators=(",", ":")).encode())
    signature = hmac.new(signing_key.encode(), payload.encode("ascii"), hashlib.sha256).digest()
    return f"{payload}.{_encode(signature)}"
