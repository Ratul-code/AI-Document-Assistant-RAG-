"""Tests for the conversation-aware orchestrator.

Verifies:

* User message is persisted immediately on submit.
* Assistant message is persisted on AnalysisDoneEvent with citations.
* Assistant message is persisted on AnalysisErrorEvent with error_code.
* Cancellation mid-stream does NOT persist a partial assistant row.
* Title is auto-set from the first question.
* Scope resolution: explicit override > all-docs flag > saved selection.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import AsyncIterator
from uuid import UUID, uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

os.environ.setdefault("ANALYSIS_LLM_API_KEY", "sk-test")


# ---------------------------------------------------------------------
# Fakes — minimal in-memory store mimicking the parts of the
# conversation repo that the orchestrator touches.
# ---------------------------------------------------------------------


class _FakeConversationStore:
    """Stores Conversation + Message rows in plain dicts.

    The orchestrator only calls four methods on the repo:

      - ``get_conversation`` (returns the conv or None)
      - ``insert_message``   (returns the inserted row)
      - ``list_conversation_document_ids``
      - ``list_ready_documents`` — handled via patch on document_repo

    We intercept those by patching the symbols the orchestrator
    imported into its module namespace.
    """

    def __init__(self, conv, *, doc_ids: list[UUID] | None = None) -> None:
        from datetime import datetime, timezone

        self.conv = conv
        self.doc_ids = doc_ids or []
        self.messages: list[tuple[str, str, list | None, str | None]] = []
        self.next_id = 0

        # last_message_at defaults to now
        self.conv.last_message_at = datetime.now(tz=timezone.utc)

    async def get_conversation(self, db, conversation_id, *, owner_id):
        if conversation_id == self.conv.id and owner_id == self.conv.owner_id:
            return self.conv
        return None

    async def insert_message(
        self, db, *, conversation_id, owner_id, role, content,
        citations=None, error_code=None,
    ):
        if conversation_id != self.conv.id:
            return None
        row = SimpleNamespace(
            id=uuid4(),
            conversation_id=conversation_id,
            role=role,
            content=content,
            citations=citations,
            error_code=error_code,
        )
        self.messages.append((role.value, content, citations, error_code))
        from datetime import datetime, timezone
        self.conv.last_message_at = datetime.now(tz=timezone.utc)
        return row

    async def list_conversation_document_ids(
        self, db, *, conversation_id, owner_id
    ):
        return list(self.doc_ids)


# ---------------------------------------------------------------------
# LLM + embedding fakes (reused from the end-to-end tests).
# ---------------------------------------------------------------------


from app.repositories.embedding_search import CandidateRow  # noqa: E402
from app.services.ai.embeddings import EmbeddingResult  # noqa: E402
from app.services.ai.llm import LLMMessage  # noqa: E402


class _FakeEmbeddingModel:
    def __init__(self, *, dim: int = 4):
        self.dim = dim
        self.model_name = "fake-embed"

    def embed(self, text: str) -> EmbeddingResult:
        return EmbeddingResult(
            vector=[1.0, 0.0, 0.0, 0.0][: self.dim],
            dim=self.dim,
            model_name=self.model_name,
        )

    def embed_batch(self, texts):
        return [self.embed(t) for t in texts]


class _FakeLLMProvider:
    def __init__(self, tokens: list[str], *, raise_on_stream: Exception | None = None):
        self._tokens = tokens
        self._raise = raise_on_stream

    async def stream(self, messages: list[LLMMessage]) -> AsyncIterator[str]:
        if self._raise is not None:
            raise self._raise
        for t in self._tokens:
            yield t


def _candidate(vector: list[float] | None = None) -> CandidateRow:
    return CandidateRow(
        chunk_id=uuid4(),
        document_id=uuid4(),
        document_title="Doc",
        page_id=uuid4(),
        page_number=0,
        chunk_index=0,
        section_path=[],
        text="Doc body.",
        vector=vector or [1.0, 0.0, 0.0, 0.0],
        embedding_model_name="fake-embed",
        embedding_dim=4,
    )


def _patch_pipeline(monkeypatch, store, *, candidates_rows, llm, ready_doc_ids=()):
    """Patch the four repo/db touchpoints the orchestrator uses."""
    import app.services.document.analyser as analyser_pkg
    import app.db.sync_session as sync_mod
    import app.repositories.document as document_mod

    # Candidate-row fetcher.
    def _cand(db, *, owner_id, document_ids=None):
        return list(candidates_rows)

    monkeypatch.setattr(analyser_pkg, "fetch_candidate_rows", _cand)

    # Sync-session shim.
    monkeypatch.setattr(
        sync_mod, "run_in_sync_session",
        lambda fn, *a, **kw: fn(None, *a, **kw),
    )

    # list_ready_documents (used by scope resolution in all-docs mode).
    rows = [SimpleNamespace(id=v) for v in ready_doc_ids]
    async def _ready(db, *, owner_id):
        return list(rows)

    monkeypatch.setattr(document_mod, "list_ready_documents", _ready)

    # Conversation repo methods.
    monkeypatch.setattr(analyser_pkg.conversation_repo, "get_conversation", store.get_conversation)
    monkeypatch.setattr(analyser_pkg.conversation_repo, "insert_message", store.insert_message)
    monkeypatch.setattr(
        analyser_pkg.conversation_repo,
        "list_conversation_document_ids",
        store.list_conversation_document_ids,
    )


@pytest.fixture
def make_conv():
    """Build a fake Conversation row."""
    from app.models.conversation import Conversation, MessageRole

    def _make(*, owner_id=None, scope_all_documents=False, title=None):
        return SimpleNamespace(
            id=uuid4(),
            owner_id=owner_id or uuid4(),
            title=title,
            scope_all_documents=scope_all_documents,
            last_message_at=None,  # store fills this
            updated_at=None,
        )

    return _make


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_persists_user_and_assistant_with_citations(
    monkeypatch, make_conv
):
    conv = make_conv()
    store = _FakeConversationStore(conv, doc_ids=[uuid4()])

    llm = _FakeLLMProvider(tokens=["Hello", " ", "world"])
    _patch_pipeline(
        monkeypatch, store,
        candidates_rows=[_candidate()],
        llm=llm,
    )

    import app.services.document.analyser as analyser_pkg
    from app.services.document.analyser import (
        AnalysisDoneEvent, AnalysisErrorEvent, AnalysisTokenEvent,
        analyse_query_with_conversation,
    )

    tokens: list[str] = []
    terminal = None
    async for ev in analyse_query_with_conversation(
        db_async=None,
        owner_id=conv.owner_id,
        question="What is X?",
        stream_id=uuid4(),
        conversation_id=conv.id,
        llm_provider=llm,
        embedding_model=_FakeEmbeddingModel(),
    ):
        if isinstance(ev, AnalysisTokenEvent):
            tokens.append(ev.text)
            assert ev.conversation_id == conv.id
        elif isinstance(ev, (AnalysisDoneEvent, AnalysisErrorEvent)):
            terminal = ev
            assert ev.conversation_id == conv.id

    assert "".join(tokens) == "Hello world"
    assert isinstance(terminal, AnalysisDoneEvent)
    assert terminal.answer == "Hello world"
    assert terminal.conversation_id == conv.id

    # User row inserted first, then assistant row.
    assert len(store.messages) == 2
    assert store.messages[0][0] == "user"
    assert store.messages[0][1] == "What is X?"
    assert store.messages[1][0] == "assistant"
    assert store.messages[1][1] == "Hello world"
    # Citations were attached to the assistant row.
    assert store.messages[1][2] is not None
    assert isinstance(store.messages[1][2], list)

    # Auto-titled.
    assert conv.title == "What is X?"


@pytest.mark.asyncio
async def test_provider_error_persists_assistant_with_error_code(
    monkeypatch, make_conv
):
    conv = make_conv()
    store = _FakeConversationStore(conv, doc_ids=[uuid4()])
    llm = _FakeLLMProvider(
        tokens=[], raise_on_stream=RuntimeError("LLM down")
    )
    _patch_pipeline(
        monkeypatch, store,
        candidates_rows=[_candidate()],
        llm=llm,
    )

    from app.services.document.analyser import (
        AnalysisDoneEvent, AnalysisErrorEvent,
        analyse_query_with_conversation,
    )

    terminal = None
    async for ev in analyse_query_with_conversation(
        db_async=None,
        owner_id=conv.owner_id,
        question="Hi",
        stream_id=uuid4(),
        conversation_id=conv.id,
        llm_provider=llm,
        embedding_model=_FakeEmbeddingModel(),
    ):
        if isinstance(ev, (AnalysisDoneEvent, AnalysisErrorEvent)):
            terminal = ev

    assert isinstance(terminal, AnalysisErrorEvent)
    assert terminal.code == "PROVIDER_ERROR"

    # User message + assistant error row.
    assert len(store.messages) == 2
    assert store.messages[0][0] == "user"
    assert store.messages[1][0] == "assistant"
    assert store.messages[1][1] == ""          # empty content
    assert store.messages[1][3] == "PROVIDER_ERROR"  # error_code
    assert store.messages[1][2] is None         # no citations


@pytest.mark.asyncio
async def test_no_documents_yields_no_documents_ready(
    monkeypatch, make_conv
):
    """If the saved scope is empty AND no override, persist user row +
    yield NO_DOCUMENTS_READY so the UI can prompt for doc selection."""
    conv = make_conv()
    store = _FakeConversationStore(conv, doc_ids=[])
    _patch_pipeline(
        monkeypatch, store,
        candidates_rows=[],   # would-be candidates don't matter — short-circuit
        llm=_FakeLLMProvider(tokens=[]),
    )

    from app.services.document.analyser import (
        AnalysisDoneEvent, AnalysisErrorEvent,
        analyse_query_with_conversation,
    )

    terminal = None
    async for ev in analyse_query_with_conversation(
        db_async=None,
        owner_id=conv.owner_id,
        question="Hi",
        stream_id=uuid4(),
        conversation_id=conv.id,
        llm_provider=_FakeLLMProvider(tokens=[]),
        embedding_model=_FakeEmbeddingModel(),
    ):
        if isinstance(ev, (AnalysisDoneEvent, AnalysisErrorEvent)):
            terminal = ev

    assert isinstance(terminal, AnalysisErrorEvent)
    assert terminal.code == "NO_DOCUMENTS_READY"
    # User row was still inserted; no assistant row.
    assert len(store.messages) == 1
    assert store.messages[0][0] == "user"


@pytest.mark.asyncio
async def test_unknown_conversation_yields_unauthorized(
    monkeypatch, make_conv
):
    conv = make_conv()
    store = _FakeConversationStore(conv, doc_ids=[uuid4()])
    _patch_pipeline(
        monkeypatch, store,
        candidates_rows=[_candidate()],
        llm=_FakeLLMProvider(tokens=[]),
    )

    from app.services.document.analyser import (
        AnalysisDoneEvent, AnalysisErrorEvent,
        analyse_query_with_conversation,
    )

    terminal = None
    async for ev in analyse_query_with_conversation(
        db_async=None,
        owner_id=uuid4(),  # not the owner of conv
        question="Hi",
        stream_id=uuid4(),
        conversation_id=conv.id,
        llm_provider=_FakeLLMProvider(tokens=[]),
        embedding_model=_FakeEmbeddingModel(),
    ):
        if isinstance(ev, (AnalysisDoneEvent, AnalysisErrorEvent)):
            terminal = ev

    assert isinstance(terminal, AnalysisErrorEvent)
    assert terminal.code == "UNAUTHORIZED"
    # No rows persisted — neither user nor assistant.
    assert store.messages == []


@pytest.mark.asyncio
async def test_override_document_ids_used_for_scope(
    monkeypatch, make_conv
):
    """`document_ids` on the submit body overrides the saved scope.

    The orchestrator forwards `document_ids` into `analyse_query`,
    which uses it as the retrieval scope. We verify by checking that
    `fetch_candidate_rows` is called with our override, not the
    conversation's saved selection.
    """
    conv = make_conv()
    store = _FakeConversationStore(conv, doc_ids=[uuid4()])
    override_id = uuid4()
    captured_scope: dict = {}

    import app.services.document.analyser as analyser_pkg
    import app.db.sync_session as sync_mod
    import app.repositories.document as document_mod

    def _cand(db, *, owner_id, document_ids=None):
        captured_scope["document_ids"] = document_ids
        return [_candidate()] if document_ids else []

    monkeypatch.setattr(analyser_pkg, "fetch_candidate_rows", _cand)
    monkeypatch.setattr(
        sync_mod, "run_in_sync_session",
        lambda fn, *a, **kw: fn(None, *a, **kw),
    )
    async def _ready(db, *, owner_id):
        return []
    monkeypatch.setattr(document_mod, "list_ready_documents", _ready)
    monkeypatch.setattr(analyser_pkg.conversation_repo, "get_conversation", store.get_conversation)
    monkeypatch.setattr(analyser_pkg.conversation_repo, "insert_message", store.insert_message)
    monkeypatch.setattr(
        analyser_pkg.conversation_repo,
        "list_conversation_document_ids",
        store.list_conversation_document_ids,
    )

    from app.services.document.analyser import analyse_query_with_conversation

    async for _ev in analyse_query_with_conversation(
        db_async=None,
        owner_id=conv.owner_id,
        question="Hi",
        stream_id=uuid4(),
        conversation_id=conv.id,
        document_ids=[override_id],   # <-- the override
        llm_provider=_FakeLLMProvider(tokens=["ok"]),
        embedding_model=_FakeEmbeddingModel(),
    ):
        pass

    assert captured_scope["document_ids"] == [override_id]


@pytest.mark.asyncio
async def test_cancellation_does_not_persist_assistant(
    monkeypatch, make_conv
):
    """If the consumer of the async generator breaks out early (e.g.
    client disconnects), the orchestrator must NOT persist a partial
    assistant row."""
    conv = make_conv()
    store = _FakeConversationStore(conv, doc_ids=[uuid4()])

    # LLM that yields forever — gives the consumer time to cancel.
    class _InfiniteLLM:
        async def stream(self, messages):
            while True:
                yield "."

    import app.services.document.analyser as analyser_pkg
    import app.db.sync_session as sync_mod
    import app.repositories.document as document_mod

    monkeypatch.setattr(analyser_pkg, "fetch_candidate_rows", lambda *a, **kw: [_candidate()])
    monkeypatch.setattr(
        sync_mod, "run_in_sync_session",
        lambda fn, *a, **kw: fn(None, *a, **kw),
    )
    async def _ready(db, *, owner_id):
        return []
    monkeypatch.setattr(document_mod, "list_ready_documents", _ready)
    monkeypatch.setattr(analyser_pkg.conversation_repo, "get_conversation", store.get_conversation)
    monkeypatch.setattr(analyser_pkg.conversation_repo, "insert_message", store.insert_message)
    monkeypatch.setattr(
        analyser_pkg.conversation_repo,
        "list_conversation_document_ids",
        store.list_conversation_document_ids,
    )

    from app.services.document.analyser import (
        AnalysisDoneEvent, analyse_query_with_conversation,
    )

    # Consume only one token, then break out.
    broken_early = False
    try:
        async for _ev in analyse_query_with_conversation(
            db_async=None,
            owner_id=conv.owner_id,
            question="Hi",
            stream_id=uuid4(),
            conversation_id=conv.id,
            llm_provider=_InfiniteLLM(),
            embedding_model=_FakeEmbeddingModel(),
        ):
            broken_early = True
            break
    except Exception:
        pass

    assert broken_early
    # User row was inserted before the stream started, but no assistant row.
    assert len(store.messages) == 1
    assert store.messages[0][0] == "user"
