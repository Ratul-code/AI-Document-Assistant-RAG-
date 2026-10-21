"""Pydantic schemas for the document upload flow.

These define what the client sends in the upload-intent request body and
what the server returns. They intentionally do NOT include the JWT or any
secret material — the client authenticates via the access_token cookie.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.document import DocumentStatus

# ----------------------------------------------------------------------
# Request: POST /api/v1/documents/upload-intent
# ----------------------------------------------------------------------


class UploadIntentRequest(BaseModel):
    """Body of the upload-intent request.

    The client tells us what file it's about to upload so we can:
    1. Validate the file type and size before issuing a signed URL.
    2. Create a `documents` row in PROCESSING state so the document has
       an id (and a WebSocket event can reference it later).
    3. Pre-flight quota checks (max docs, max storage per user).

    The file itself is NOT in this body — it goes directly to Supabase.
    """

    title: Annotated[str, Field(min_length=1, max_length=200)]
    filename: Annotated[str, Field(min_length=1, max_length=255)]
    mime_type: Annotated[str, Field(min_length=1, max_length=127)]
    file_size_bytes: Annotated[int, Field(gt=0)]

    @field_validator("filename")
    @classmethod
    def _strip_filename(cls, v: str) -> str:
        """Trim whitespace from filenames; reject empty after trim."""
        v = v.strip()
        if not v:
            raise ValueError("filename cannot be empty")
        return v


# ----------------------------------------------------------------------
# Response: POST /api/v1/documents/upload-intent
# ----------------------------------------------------------------------


class UploadIntentResponse(BaseModel):
    """What we return after creating the intent and signing the URL.

    The client then PUTs the file to `upload_url` (direct to Supabase),
    and calls `POST /documents/{document_id}/confirm` when done.
    """

    document_id: UUID
    upload_url: str
    storage_path: str
    status: DocumentStatus
    # NOTE: Supabase presigned upload URLs are valid for 2 hours by
    # platform policy — we don't expose or promise a different TTL.


# ----------------------------------------------------------------------
# Response: POST /api/v1/documents/{id}/confirm
# ----------------------------------------------------------------------


class ConfirmUploadResponse(BaseModel):
    """What we return after resolving an upload (success OR failure).

    Used by BOTH `POST /{id}/confirm` (upload succeeded) and
    `POST /{id}/upload-failed` (upload failed). Because a failed upload
    never had a successful PUT, `uploaded_at` is naturally None in
    that case — and even on a successful re-confirm of an already-uploaded
    doc it may be None until the row is re-read. So the field is
    optional and callers should treat None as "no successful upload yet".

    The actual AI processing is NOT started in either handler yet — that
    will be a Celery task wired up later.
    """

    document_id: UUID
    status: DocumentStatus
    uploaded_at: datetime | None = None


# ----------------------------------------------------------------------
# Request: POST /api/v1/documents/{id}/upload-failed
# ----------------------------------------------------------------------


class UploadFailedRequest(BaseModel):
    """Body of the upload-failed report.

    The client calls this when its PUT to the presigned Supabase URL
    fails — typically a network error, a CORS block, a 403 from a stale
    URL, or an aborted upload. We mark the document FAILED so it doesn't
    linger in PROCESSING forever and so the UI can show an inline error.

    Idempotent: a later call (or a call after a successful /confirm) is
    a no-op rather than an error, so retries from a flaky client are safe.
    """

    # 500 chars cap on the wire — the DB column allows 1000, but we don't
    # need a full stack trace in the API surface; keep it readable.
    error_message: Annotated[str, Field(min_length=1, max_length=500)]
    # The HTTP status code the client saw from Supabase, if any. Helps
    # debug 403 vs 500 vs network errors without parsing message text.
    http_status: int | None = Field(default=None, ge=100, le=599)


# ----------------------------------------------------------------------
# Response: GET /api/v1/documents
# ----------------------------------------------------------------------


class DocumentListItem(BaseModel):
    """One row in the document list.

    Includes what the client needs to render a "My Documents" page and
    to power a "pick a doc to chat with" picker:
    - id and title for the row / option label
    - filename + mime_type for an icon
    - file_size_bytes for a human-readable size label
    - status + error_message for the status badge / inline error
    - timestamps for the row subtitle

    Internal fields (storage_bucket, storage_path, file_hash, owner_id)
    are deliberately omitted — the client doesn't need them, and we
    don't want to leak Supabase paths.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    filename: str
    mime_type: str
    file_size_bytes: int
    status: DocumentStatus
    error_message: str | None = None
    uploaded_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class DocumentListResponse(BaseModel):
    """Paginated list of documents owned by the caller.

    `total` is the count across ALL matching documents for this user
    (respecting the `status` filter), so the UI can render
    "Showing 1–20 of 137" without a second request.
    """

    items: list[DocumentListItem]
    total: int
    limit: int
    offset: int


# ----------------------------------------------------------------------
# Request: PATCH /api/v1/documents/{id}
# ----------------------------------------------------------------------


class DocumentUpdate(BaseModel):
    """Body of `PATCH /api/v1/documents/{id}`.

    Currently the only editable user-facing attribute is the title —
    the human-readable label the UI shows on each row. The original
    filename (the file's name on disk) is NOT editable: it's a
    property of the uploaded file, not a label, and renaming it would
    create a mismatch between what's stored in Supabase Storage and
    what the row claims the file is called.

    All fields are optional so the route can support partial updates
    cleanly. Today there's only `title`; future editable attributes
    (description, tags, etc.) would be added here as optional fields
    without breaking existing callers.
    """

    title: Annotated[str, Field(min_length=1, max_length=200)] | None = None


# ----------------------------------------------------------------------
# Response: GET /api/v1/documents/{id}/stats
# ----------------------------------------------------------------------


class DocumentStatsResponse(BaseModel):
    """Per-document aggregate counts surfaced by the detail dialog.

    Powers `GET /api/v1/documents/{id}/stats`. The list endpoint
    intentionally returns a narrow `DocumentListItem` shape (see its
    docstring) — broadening it would inflate every list payload and
    cause N+1 work for the list page that doesn't need it. A dedicated
    per-document stats endpoint lets the detail dialog get the full
    picture with a single round-trip on demand.

    All counts are derived from the existing `document_pages`,
    `document_chunks`, and `document_embeddings` tables; no new tables
    or migrations are needed.

    Fields:
      - `document`: the same `DocumentListItem` the list endpoint
        returns, so the dialog can render its header in one call.
      - `page_count`: number of rows in `document_pages` for this doc.
      - `chunks_total`: number of rows in `document_chunks` for this doc.
      - `chunks_embedded`: chunks whose embedding row has status
        `succeeded`. The "doc is queryable" signal.
      - `chunks_pending`: chunks with `needs_embedding=True` and no
        succeeded embedding — i.e. the embedding worker still owes work.
      - `chunks_failed`: chunks whose embedding row has status `failed`.
        A non-zero value here usually means a re-run is needed.
      - `pages_needing_ocr`: pages whose `ocr_status` is anything other
        than `not_needed` (i.e. `pending` / `processing` / `succeeded` /
        `failed` / `skipped`). The OCR worker hasn't shipped yet, so
        today this is always 0; the field is here so the dialog
        doesn't have to special-case the empty state.
      - `pages_ocr_pending`: pages with `ocr_status in {pending,
        processing}` — i.e. OCR work in flight.
      - `pages_ocr_failed`: pages with `ocr_status == failed`.
      - `embedding_models`: distinct `model_name` values currently
        backing this doc's embeddings, ordered alphabetically. Most
        docs have exactly one entry; the list makes the rare mixed-
        model case (re-runs after a model swap) visible.
      - `embedding_status_counts`: histogram of `document_embeddings.status`
        values for this doc. Lets the dialog render the "X succeeded,
        Y failed" breakdown without baking in a fixed set of statuses.
    """

    model_config = ConfigDict(from_attributes=True)

    document: DocumentListItem

    page_count: int
    chunks_total: int
    chunks_embedded: int
    chunks_pending: int
    chunks_failed: int

    pages_needing_ocr: int
    pages_ocr_pending: int
    pages_ocr_failed: int

    embedding_models: list[str]
    embedding_status_counts: dict[str, int]
