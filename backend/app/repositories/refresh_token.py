"""Refresh-token queries."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.refresh_token import RefreshToken


async def get_by_hash(db: AsyncSession, token_hash: str) -> RefreshToken | None:
    """Look up a refresh token by its SHA-256 hash."""
    stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def create_refresh_token(
    db: AsyncSession,
    *,
    user_id: UUID,
    token_hash: str,
    family_id: UUID,
    expires_at: datetime,
) -> RefreshToken:
    """Insert a new refresh-token row."""
    row = RefreshToken(
        user_id=user_id,
        token_hash=token_hash,
        family_id=family_id,
        expires_at=expires_at,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def mark_used(db: AsyncSession, token: RefreshToken) -> None:
    """Record that the token was just used (for auditing / debugging)."""
    token.last_used_at = datetime.now(timezone.utc)
    await db.flush()


async def revoke(db: AsyncSession, token: RefreshToken) -> None:
    """Mark a single token as revoked."""
    token.revoked_at = datetime.now(timezone.utc)
    await db.flush()


async def revoke_family(db: AsyncSession, family_id: UUID) -> None:
    """Revoke every non-revoked row in a token family (reuse-detection)."""
    now = datetime.now(timezone.utc)
    stmt = (
        update(RefreshToken)
        .where(
            RefreshToken.family_id == family_id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    await db.execute(stmt)


async def set_replaced_by(
    db: AsyncSession, old: RefreshToken, new_id: UUID
) -> None:
    """Link a rotated-out token to the row that replaced it."""
    old.replaced_by_id = new_id
    await db.flush()