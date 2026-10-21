"""Authentication service: register, login, refresh, logout.

This layer orchestrates repositories + security helpers. Routes should be thin
and delegate here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.repositories import refresh_token as refresh_repo
from app.repositories import user as user_repo


class AuthError(Exception):
    """Raised for any auth failure that should surface as a 401."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(slots=True)
class TokenPair:
    """Result of a successful login/refresh."""

    access_token: str
    refresh_token: str  # raw token — only returned to the caller once


@dataclass(slots=True)
class IssuedTokens:
    """Tokens + cookie attributes set by the route layer."""

    tokens: TokenPair
    access_max_age_seconds: int
    refresh_max_age_seconds: int
    user_id: UUID



# --- Register --------------------------------------------------------------

async def register_user(
    db: AsyncSession,
    *,
    name: str,
    email: str,
    password: str,
) -> User:
    """Create a new user. Lowercases the email; raises AuthError on conflict."""
    normalized_email = email.lower().strip()
    if await user_repo.get_by_email(db, normalized_email) is not None:
        raise AuthError("Email is already registered")

    user = await user_repo.create_user(
        db,
        name=name.strip(),
        email=normalized_email,
        hashed_password=hash_password(password),
    )
    return user


# --- Login -----------------------------------------------------------------

async def login(db: AsyncSession, *, email: str, password: str) -> tuple[User, IssuedTokens]:
    """Verify credentials and issue a fresh token pair.

    Returns the user (so the route can serialize a UserRead without a second
    DB round-trip) plus the issued tokens. Each successful login starts a
    new token family (independent session).
    """
    normalized_email = email.lower().strip()
    user = await user_repo.get_by_email(db, normalized_email)
    if user is None or not verify_password(password, user.hashed_password):
        raise AuthError("Invalid email or password")
    if not user.is_active:
        raise AuthError("User account is disabled")

    issued = (await _issue_pair(db, user=user, family_id=uuid4())).issued
    return user, issued


# --- Refresh ---------------------------------------------------------------

async def refresh(db: AsyncSession, *, raw_refresh_token: str) -> IssuedTokens:
    """Rotate a refresh token.

    Behavior:
      1. Hash the incoming token and look it up.
      2. If not found → reject.
      3. If found but revoked or expired → reject (and revoke the family if it
         was reused after revocation — defense against token theft).
      4. Otherwise: revoke this row, mint a new pair in the same family, and
         link them via replaced_by_id.
    """
    settings = get_settings()
    token_hash = hash_refresh_token(raw_refresh_token)
    existing = await refresh_repo.get_by_hash(db, token_hash)

    if existing is None:
        raise AuthError("Invalid refresh token")

    now = datetime.now(timezone.utc)
    expires_at = existing.expires_at
    if expires_at.tzinfo is None:
        # SQLite returns timezone-aware columns as naive datetimes.
        expires_at = expires_at.replace(tzinfo=timezone.utc)    

    revoked_at = existing.revoked_at
    if revoked_at is not None and revoked_at.tzinfo is None:
        revoked_at = revoked_at.replace(tzinfo=timezone.utc)

    if revoked_at is not None or expires_at <= now:
        # Token was already used or expired. If it was revoked (rotated), this
        # is a reuse event — nuke the entire family.
        if existing.revoked_at is not None:
            await refresh_repo.revoke_family(db, existing.family_id)
        raise AuthError("Refresh token is no longer valid")

    user = await user_repo.get_by_id(db, existing.user_id)
    if user is None or not user.is_active:
        raise AuthError("User account is unavailable")

    # Revoke the incoming token and mint a new pair in the same family.
    await refresh_repo.revoke(db, existing)
    new_pair = await _issue_pair(
        db,
        user=user,
        family_id=existing.family_id,
    )
    # Point the just-revoked row at the freshly-created one for audit trail.
    await refresh_repo.set_replaced_by(
        db, old=existing, new_id=new_pair.new_token_row_id
    )
    return new_pair.issued


# --- Logout ----------------------------------------------------------------

async def logout(db: AsyncSession, *, raw_refresh_token: str) -> None:
    """Revoke a single refresh token. Idempotent: missing tokens are fine."""
    token_hash = hash_refresh_token(raw_refresh_token)
    existing = await refresh_repo.get_by_hash(db, token_hash)
    if existing is not None and existing.revoked_at is None:
        await refresh_repo.revoke(db, existing)



@dataclass(slots=True)
class _IssuedInternal:
    """Internal: holds the issued pair plus the new DB row id for back-linking."""

    issued: IssuedTokens
    new_token_row_id: UUID




# --- Helpers ---------------------------------------------------------------

    
async def _issue_pair(
    db: AsyncSession,
    *,
    user: User,
    family_id: UUID,
    ttl_seconds: int | None = None,
) -> _IssuedInternal:
    """Build an access JWT, generate + persist a refresh token, return both."""
    settings = get_settings()
    refresh_ttl = (
        ttl_seconds
        if ttl_seconds is not None
        else settings.refresh_token_ttl_seconds
    )

    raw_refresh = generate_refresh_token()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=refresh_ttl)

    row = await refresh_repo.create_refresh_token(
        db,
        user_id=user.id,
        token_hash=hash_refresh_token(raw_refresh),
        family_id=family_id,
        expires_at=expires_at,
    )

    access = create_access_token(
        subject=user.id,
        extra_claims={"email": user.email},
    )
    return _IssuedInternal(
        issued=IssuedTokens(
            tokens=TokenPair(access_token=access, refresh_token=raw_refresh),
            access_max_age_seconds=settings.access_token_ttl_seconds,
            refresh_max_age_seconds=refresh_ttl,
            user_id=user.id,
        ),
        new_token_row_id=row.id,
    )





