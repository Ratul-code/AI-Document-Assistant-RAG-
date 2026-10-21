"""Pre-flight validation for the upload-intent flow.

Runs BEFORE we touch Supabase or the database so the user gets a fast,
clear 4xx response instead of paying for a useless signed URL or
leaving a half-created row behind.

Limits enforced here:
- MIME type is in the configured allowlist (defaults to pdf/docx/txt/md).
- File size is within the configured maximum.
- User is under the per-user document count cap.
- User is under the per-user total storage cap.

The exact limits live in app.core.config.Settings; this module only
applies them. Bucket-level MIME restrictions on Supabase act as a
second line of defence (see Supabase dashboard).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.repositories import document as document_repo

# Per-user hard caps. These are not configurable yet (small project),
# but kept as constants so they're easy to find and bump.
_MAX_DOCUMENTS_PER_USER = 100
_MAX_STORAGE_BYTES_PER_USER = 1 * 1024 * 1024 * 1024  # 1 GiB


@dataclass(frozen=True)
class UploadValidationResult:
    """What we computed during validation. Returned for logging/testing."""

    mime_type: str
    file_size_bytes: int
    current_doc_count: int
    current_storage_bytes: int


def validate_mime_type(mime_type: str) -> None:
    """Raise 415 if the client claimed a MIME we don't accept.

    The Supabase bucket will also enforce this, but checking here gives
    the user a clear error before we do any work.
    """
    allowed = get_settings().upload_allowed_mime_types_list
    if mime_type not in allowed:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Unsupported file type: {mime_type!r}. " f"Allowed types: {', '.join(allowed)}"
            ),
        )


def validate_file_size(file_size_bytes: int) -> None:
    """Raise 413 if the file is bigger than the configured maximum."""
    max_bytes = get_settings().upload_max_size_bytes
    if file_size_bytes > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"File too large: {file_size_bytes} bytes. " f"Max allowed: {max_bytes} bytes."
            ),
        )


async def validate_quotas(
    db: AsyncSession,
    *,
    owner_id: UUID,
    incoming_bytes: int,
) -> UploadValidationResult:
    """Raise 429 if the user would exceed the per-user caps.

    Both checks happen against current state, BEFORE we insert the new
    document row. The new row will be inserted only if both checks pass.
    """
    current_count = await document_repo.count_user_documents(db, owner_id=owner_id)
    current_bytes = await document_repo.total_user_storage_bytes(db, owner_id=owner_id)

    if current_count >= _MAX_DOCUMENTS_PER_USER:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Document limit reached "
                f"({current_count}/{_MAX_DOCUMENTS_PER_USER}). "
                f"Delete an existing document before uploading more."
            ),
        )

    projected_bytes = current_bytes + incoming_bytes
    if projected_bytes > _MAX_STORAGE_BYTES_PER_USER:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Storage quota exceeded. "
                f"Current: {current_bytes} bytes, "
                f"incoming: {incoming_bytes} bytes, "
                f"max: {_MAX_STORAGE_BYTES_PER_USER} bytes."
            ),
        )

    return UploadValidationResult(
        mime_type="",  # filled by caller for clarity; not used by quota
        file_size_bytes=incoming_bytes,
        current_doc_count=current_count,
        current_storage_bytes=current_bytes,
    )


async def validate_upload_intent(
    db: AsyncSession,
    *,
    owner_id: UUID,
    mime_type: str,
    file_size_bytes: int,
) -> None:
    """Run all pre-flight checks in order. Raises HTTPException on failure.

    Order matters slightly: cheap checks first (mime, size), then DB
    round-trips (quota).
    """
    validate_mime_type(mime_type)
    validate_file_size(file_size_bytes)
    await validate_quotas(db, owner_id=owner_id, incoming_bytes=file_size_bytes)
