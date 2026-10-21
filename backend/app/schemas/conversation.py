"""Pydantic schemas for conversations and messages.

Conversation-scoped chat UI lives here:

* `ConversationCreate`  — body for `POST /conversations`.
* `ConversationUpdate`  — body for `PATCH /conversations/{id}`.
* `ConversationRead`    — full metadata + document selection.
* `ConversationListItem` / `ConversationListResponse` — sidebar list.
* `ConversationMessageRead` — one turn (user OR assistant) in the history.

`QueryRequest` (in `schemas/analysis.py`) is the body for submitting a
question to a conversation; it gained an optional `conversation_id`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.analysis import AnalysisErrorCode, QueryCitation


# ---------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------


class ConversationCreate(BaseModel):
    """Body for `POST /conversations`.

    Document selection rules:
      - `scope_all_documents=True` → ignore `document_ids`, ground every
        turn on all of the caller's READY documents.
      - `scope_all_documents=False` + `document_ids=[]` → no scope yet;
        every query will return NO_DOCUMENTS_READY until the user picks.
      - `scope_all_documents=False` + non-empty `document_ids` → ground
        on those (each must be owned by caller and READY).
    """

    title: Annotated[str | None, Field(default=None, max_length=255)] = None
    document_ids: list[UUID] = Field(default_factory=list)
    scope_all_documents: bool = False

    @field_validator("document_ids")
    @classmethod
    def _dedupe_document_ids(cls, v: list[UUID]) -> list[UUID]:
        """De-dupe while preserving order — UI may re-send the same doc."""
        seen: set[UUID] = set()
        out: list[UUID] = []
        for d in v:
            if d not in seen:
                seen.add(d)
                out.append(d)
        return out


# ---------------------------------------------------------------------
# Update (PATCH)
# ---------------------------------------------------------------------


class ConversationUpdate(BaseModel):
    """Body for `PATCH /conversations/{id}`.

    `document_ids` semantics (only applied when the field is present):
      - `None` → leave the selection unchanged.
      - `[]`   → clear the selection (matches `scope_all_documents=False`
                 with an empty scope).
      - non-empty list → replace the selection.
    """

    title: Annotated[str | None, Field(default=None, max_length=255)] = None
    document_ids: list[UUID] | None = Field(default=None)
    scope_all_documents: bool | None = None

    @field_validator("document_ids")
    @classmethod
    def _dedupe_document_ids(cls, v: list[UUID] | None) -> list[UUID] | None:
        if v is None:
            return None
        seen: set[UUID] = set()
        out: list[UUID] = []
        for d in v:
            if d not in seen:
                seen.add(d)
                out.append(d)
        return out


# ---------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------


class ConversationRead(BaseModel):
    """Full metadata for a single conversation."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str | None
    scope_all_documents: bool
    document_ids: list[UUID] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime


# ---------------------------------------------------------------------
# Sidebar list
# ---------------------------------------------------------------------


class ConversationListItem(BaseModel):
    """One row in `GET /conversations` — minimal info for the sidebar."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str | None
    last_message_at: datetime
    # Useful for a "📎 N docs" badge next to the chat title.
    document_count: int = 0


class ConversationListResponse(BaseModel):
    """Paginated list of conversations (sidebar)."""

    items: list[ConversationListItem]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------


class ConversationMessageRead(BaseModel):
    """One turn in a conversation's history.

    `role` is `"user"` or `"assistant"`. The frontend uses it to pick
    alignment, bubble style, and (for assistant rows) whether to render
    citations and a retry button on errors.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    role: Literal["user", "assistant"]
    content: str
    citations: list[QueryCitation] = Field(default_factory=list)
    error_code: AnalysisErrorCode | None = None
    created_at: datetime


class ConversationMessageListResponse(BaseModel):
    """Paginated chat history — oldest → newest within a conversation."""

    items: list[ConversationMessageRead]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------
# Submit a question
# ---------------------------------------------------------------------


class ConversationMessageSubmit(BaseModel):
    """Body for `POST /conversations/{id}/messages`.

    Same shape as `QueryRequest` but lives on the conversation route so
    the chat UI has one clear endpoint to hit. `document_ids` is an
    override — omit it to use the conversation's saved scope.
    """

    question: Annotated[str, Field(min_length=1, max_length=2000)]
    document_ids: list[UUID] | None = Field(default=None)


__all__ = [
    "ConversationCreate",
    "ConversationListItem",
    "ConversationListResponse",
    "ConversationMessageListResponse",
    "ConversationMessageRead",
    "ConversationMessageSubmit",
    "ConversationRead",
    "ConversationUpdate",
]