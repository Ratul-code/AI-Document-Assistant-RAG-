"""Pydantic schemas for refresh-token data and auth responses."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class RefreshTokenRead(BaseModel):
    """Safe refresh-token session metadata; never expose token_hash."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    family_id: UUID
    expires_at: datetime
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    replaced_by_id: UUID | None


class AuthResponse(BaseModel):
    """Body of /login and /refresh responses.

    Tokens are NOT in the body — they're delivered as HttpOnly cookies so the
    browser holds them and JavaScript cannot read them. Non-browser clients
    (e.g. Next.js server components) should forward the `Cookie` request header
    to access protected endpoints.
    """

    user_id: UUID
    access_token_expires_in: int
    refresh_token_expires_in: int
