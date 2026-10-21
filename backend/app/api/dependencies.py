"""Reusable FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import decode_access_token
from app.db.session import get_db
from app.models.user import User
from app.repositories import user as user_repo

_settings = get_settings()
_ACCESS_COOKIE_NAME = _settings.access_cookie_name

DbDep = Annotated[AsyncSession, Depends(get_db)]


async def get_current_user(
    cookie_token: Annotated[str | None, Cookie(alias=_ACCESS_COOKIE_NAME)] = None,
    db: DbDep = None,  # type: ignore[assignment]
) -> User:
    """Resolve the user from the access-token cookie.

    The cookie is OPTIONAL (default=None): when missing, we raise 401 instead
    of letting FastAPI return 422.

    Raises 401 if the cookie is missing, expired, malformed, or the user
    no longer exists / is disabled.
    """
    if not cookie_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    try:
        payload = decode_access_token(cookie_token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
        )
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
        )

    sub = payload.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )

    try:
        user_id = UUID(sub)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid subject in token",
        )

    user = await user_repo.get_by_id(db, user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or disabled",
        )
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]


# --- Optional auth (for routes that behave differently when logged in) ---

async def get_current_user_optional(
    cookie_token: Annotated[str | None, Cookie(alias=_ACCESS_COOKIE_NAME)] = None,
    db: DbDep = None,  # type: ignore[assignment]
) -> User | None:
    """Same as get_current_user but returns None when no/invalid token."""
    try:
        return await get_current_user(cookie_token, db)
    except HTTPException:
        return None


OptionalUserDep = Annotated[User | None, Depends(get_current_user_optional)]
