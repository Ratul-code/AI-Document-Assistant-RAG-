"""Security helpers: password hashing, JWT signing, refresh-token hashing."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from app.core.config import get_settings

# bcrypt rounds. 12 is the common default (~250ms on modern hardware).
# Bump to 13+ if your CPU is fast and you want extra margin.
_BCRYPT_ROUNDS = 12


# --- Passwords -------------------------------------------------------------

def _to_bytes(s: str) -> bytes:
    """Bcrypt requires bytes; the bcrypt>=4 binding rejects >72 bytes
    inputs outright, so we hash first to avoid that limit."""
    # Encode, then SHA-256 to normalize length before bcrypt.
    # This is the standard "pre-hash before bcrypt" trick that lets users
    # have long passphrases without hitting bcrypt's 72-byte ceiling.
    return hashlib.sha256(s.encode("utf-8")).hexdigest().encode("utf-8")


def hash_password(plain: str) -> str:
    """Return a bcrypt hash of the given plaintext password."""
    return bcrypt.hashpw(_to_bytes(plain), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time bcrypt comparison. Returns False on any decode error."""
    try:
        return bcrypt.checkpw(_to_bytes(plain), hashed.encode("utf-8"))
    except Exception:
        return False


# --- Access tokens (stateless JWT) -----------------------------------------

def create_access_token(
    subject: str | uuid.UUID,
    *,
    extra_claims: dict[str, Any] | None = None,
    ttl_seconds: int | None = None,
) -> str:
    """Sign an HS256 access JWT.

    `subject` is the user id (UUID or string). `extra_claims` is merged into
    the payload (e.g. {"email": "..."}). TTL falls back to settings.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    ttl = ttl_seconds if ttl_seconds is not None else settings.access_token_ttl_seconds

    payload: dict[str, Any] = {
        "sub": str(subject),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl)).timestamp()),
        "jti": str(uuid.uuid4()),
        "type": "access",
    }
    if extra_claims:
        payload.update(extra_claims)

    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and verify an access JWT. Raises jwt.PyJWTError on failure."""
    settings = get_settings()
    payload = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
    )
    if payload.get("type") != "access":
        raise jwt.InvalidTokenError("Token is not an access token")
    return payload


# --- Refresh tokens (stateful, hashed at rest) -----------------------------

def generate_refresh_token() -> str:
    """Cryptographically secure URL-safe random string (~43 chars)."""
    return secrets.token_urlsafe(32)


def hash_refresh_token(raw: str) -> str:
    """SHA-256 hex digest. We use SHA-256 (not bcrypt) because:
       - the input has ~256 bits of entropy from token_urlsafe, so brute-force
         is infeasible without DB access,
       - we need O(1) lookup by hash on every refresh request,
       - bcrypt is slow on purpose; we don't want that overhead per request.
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()