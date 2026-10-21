"""Pydantic schemas for the per-document activity log.

Wire shape for `GET /api/v1/documents/{id}/logs` and the
`document.log` WS event. The DB column `metadata_json` is `Text`
(stored as a JSON-encoded string per the original Alembic migration);
this schema hides that detail from the client and presents `metadata`
as a plain `dict`.

The round-trip is done in a `model_validator(mode="before")` so a
malformed JSON payload never crashes the response — we return `None`
instead, which is the same shape a successful parse returns when
there's nothing to encode.

Note on the field name `metadata`: SQLAlchemy's declarative base
injects a `.metadata` attribute on every mapped class (it's the
`MetaData()` registry for the table), so the wire field can't be
populated via `model_validate(row).metadata` — that would resolve
to SQLAlchemy's `MetaData()` object, not our JSON column. We
work around it by using `model_validator(mode="before")` to remap
`row.metadata_json` (the actual column) to the wire field `metadata`
before Pydantic tries to validate the field types.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ---------------------------------------------------------------------
# Response item
# ---------------------------------------------------------------------


class DocumentLogItem(BaseModel):
    """One row of the activity feed.

    `metadata` is a free-form dict on the wire (the DB column is a
    JSON string keyed `metadata_json` in the ORM). Each event_type
    has a conventional shape documented in `DocumentLogEventType`
    (e.g. `embedding.progress` carries `{batch_index, batch_size,
    chunks_done, chunks_total, failed_in_batch}`) but we don't
    validate the inner shape here — the worker is the only writer,
    and changing the inner keys is a frontend-agnostic backend change
    as long as the outer fields stay stable.
    """

    # `from_attributes=True` lets us call `model_validate(orm_row)`;
    # we don't actually use it for `metadata` (the validator below
    # remaps the column) but it's needed for the other fields.
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_id: UUID
    event_type: str
    message: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _coerce_metadata(cls, data: Any) -> Any:
        """Map the ORM's `metadata_json` Text column to the wire's
        `metadata` dict field, and parse the JSON string safely.

        The DB column is `Text` storing a JSON-encoded string;
        SQLAlchemy returns the raw string under the attribute name
        `metadata_json`. The wire shape is a `dict` under the name
        `metadata`. Reading `metadata` directly off the ORM row would
        return SQLAlchemy's `MetaData()` (the declarative registry),
        not our column — so we have to remap by name.

        Handles three input shapes:
          - ORM row (SQLAlchemy mapped instance) — convert to a
            dict from the explicit column attributes.
          - Plain dict — remap `metadata_json` to `metadata`.
          - Anything else — pass through; Pydantic will raise its
            own validation error if the shape is wrong.
        """
        # ORM row path: read the column attributes explicitly so we
        # don't trip over the declarative `Base.metadata` registry.
        if not isinstance(data, dict) and hasattr(data, "__table__"):
            data = {
                "id": data.id,
                "document_id": data.document_id,
                "event_type": data.event_type,
                "message": data.message,
                "metadata_json": data.metadata_json,
                "created_at": data.created_at,
            }

        if not isinstance(data, dict):
            return data

        # Wire path: already a parsed dict, leave it alone.
        if data.get("metadata") is not None:
            return data

        # ORM / DB path: remap `metadata_json` (Text column) →
        # `metadata` (dict field).
        raw = data.get("metadata_json")
        if raw is None or raw == "":
            data["metadata"] = None
            return data
        if isinstance(raw, dict):
            data["metadata"] = raw
            return data
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                data["metadata"] = None
                return data
            data["metadata"] = parsed if isinstance(parsed, dict) else None
            return data
        data["metadata"] = None
        return data


# ---------------------------------------------------------------------
# Paginated list
# ---------------------------------------------------------------------


class DocumentLogListResponse(BaseModel):
    """Paginated activity feed for one document.

    `total` is the count of ALL log rows for the document (after the
    owner-scope filter), not just the current page, so the client can
    render "Showing 1–20 of N" without a second request. `limit` and
    `offset` echo the request values so a paginating client can
    confirm what it got.
    """

    items: list[DocumentLogItem]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


__all__ = ["DocumentLogItem", "DocumentLogListResponse"]
