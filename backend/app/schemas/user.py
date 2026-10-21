"""Pydantic schemas for user input and output."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserCredential(BaseModel):
    """Data accepted when a user tries to login."""

    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class UserCreate(UserCredential):
    """Data accepted when a user is registered."""

    name: str = Field(min_length=1, max_length=255)



class UserRead(BaseModel):
    """Safe user representation returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    email: EmailStr
    is_active: bool
    is_superuser: bool
    email_verified_at: datetime | None
    created_at: datetime
    updated_at: datetime


class UserUpdate(BaseModel):
    """Optional fields that may be changed later."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    email: EmailStr | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)
