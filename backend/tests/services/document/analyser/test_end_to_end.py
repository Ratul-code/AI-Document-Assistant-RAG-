"""End-to-end analyser test with fake embedding model and stub LLM.

Verifies the wiring of:
  * Embedding the question via the injected model.
  * Dim-mismatch detection (PROVIDER_MISMATCH).
  * Scope resolution / NO_DOCUMENTS_READY short-circuit.
  * Top-K ranking against the (fake) scores.
  * Prompt construction (system + user messages).
  * LLM streaming and final `AnalysisDoneEvent`.

Real embedding and LLM models are NOT used; the test isolates the
orchestration logic.
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

from app.repositories.embedding_search import CandidateRow  # noqa: E402
from app.services.ai.embeddings import EmbeddingResult  # noqa: E402
from app.services.ai.llm import LLMMessage, LLMProvider  # noqa: E402
from app.services.document.analyser import (  # noqa: E402
    AnalysisDoneEvent,
    AnalysisErrorEvent,
    AnalysisTokenEvent,
    analyse_query,
    analyse_query_once,
)
from app.services.document.analyser.events import AnalysisEvent  # noqa: E402


# ---------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------


class _FakeEmbeddingModel:
    """Returns a fixed vector regardless of input — controlled per test."""

    def __init__(self, *, dim: int = 4, vector: list[float] | None = None) -> None:
        self.dim = dim
        self.model_name = "fake-embed"
        self._vector = vector or [1.0, 0.0, 0.0, 0.0][:dim]

    def embed(self, text: str) -> EmbeddingResult:
        return EmbeddingResult(vector=list(self._vector), dim=self.dim, model_name=self.model_name)

    def embed_batch(self, texts: list[str]) -> list[EmbeddingResult]:
        return [self.embed(t) for t in texts]


class _FakeLLMProvider:
    """Yields a fixed sequence of tokens, then completes."""

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens
        self.call_messages: list[list[LLMMessage]] = []

    async def stream(self, messages: list[LLMMessage]) -> AsyncIterator[str]:
        self.call_messages.append(messages)
        for t in self._tokens:
            yield t

    async def complete(self, messages: list[LLMMessage]) -> str:
        self.call_messages.append(messages)
        return "".join(self._tokens)


class _FakeAsyncSession:
    """An async DB stub that the analyser never actually queries
    (the candidate set comes from the test). It exists only so the
    orchestrator can be called with the right type."""


def _candidate(
    *, title: str = "Doc", vector: list[float] | None = None, page: int = 0
) -> CandidateRow:
    return CandidateRow(
        chunk_id=uuid4(),
        document_id=uuid4(),
        document_title=title,
        page_id=uuid4(),
        page_number=page,
        chunk_index=0,
        section_path=[],
        text=f"{title} body for testing.",
        vector=vector or [0.0] * 4,
        embedding_model_name="fake-embed",
        embedding_dim=4,
    )


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------


@pytest.fixture
def patched_deps(monkeypatch):
    """Reserved for future use; tests below use the `_set_candidates` /
    `_set_list_ready` helpers directly to keep each test self-contained.
    """
    yield


def _set_candidates(monkeypatch, rows):
    """Replace `fetch_candidate_rows` on the analyser package.

    The analyser package imports `fetch_candidate_rows` at module load
    via `from app.repositories.embedding_search import ...`. The bound
    name on the analyser module is what the orchestrator resolves at
    call time, so patching only the source module is not enough.
    """
    import app.services.document.analyser as analyser_pkg

    def _f(db, *, owner_id, document_ids=None):  # noqa: ARG001
        return list(rows)

    monkeypatch.setattr(analyser_pkg, "fetch_candidate_rows", _f)


def _set_list_ready(monkeypatch, value=()):
    """Replace `list_ready_documents` on the source module.

    The analyser package holds `document_repo` (the module itself), so
    `document_repo.list_ready_documents` resolves through the module
    attribute at call time — patching the source is enough.

    `value` may contain either `UUID`s or objects with an `.id`
    attribute; the analyser calls `[doc.id for doc in docs]`, so we
    normalise to `SimpleNamespace(id=...)` so the production code
    sees the same shape as real ORM rows.
    """
    import app.repositories.document as document_mod

    rows = [v if hasattr(v, "id") else SimpleNamespace(id=v) for v in value]

    async def _f(db, *, owner_id):  # noqa: ARG001
        return list(rows)

    monkeypatch.setattr(document_mod, "list_ready_documents", _f)


@pytest.mark.asyncio
async def test_no_candidates_yields_no_documents_ready(monkeypatch) -> None:
    """If the embedding-search repo returns no rows (caller selected
    nothing, or only empty docs), the orchestrator yields
    NO_DOCUMENTS_READY without calling the LLM."""
    import app.db.sync_session as sync_mod

    _set_candidates(monkeypatch, [])
    _set_list_ready(monkeypatch, [])
    monkeypatch.setattr(
        sync_mod, "run_in_sync_session",
        lambda fn, *a, **kw: fn(None, *a, **kw),
    )

    llm = _FakeLLMProvider(tokens=["unused"])
    events: list[AnalysisEvent] = []
    async for ev in analyse_query(
        db_async=_FakeAsyncSession(),
        owner_id=uuid4(),
        question="What is X?",
        document_ids=None,
        stream_id=uuid4(),
        llm_provider=llm,
    ):
        events.append(ev)
    assert isinstance(events[-1], AnalysisErrorEvent)
    assert events[-1].code == "NO_DOCUMENTS_READY"
    # LLM was never invoked because no chunks were available.
    assert llm.call_messages == []


@pytest.mark.asyncio
async def test_dim_mismatch_yields_provider_mismatch(monkeypatch) -> None:
    import app.db.sync_session as sync_mod

    # Stub the candidate fetch to return one row whose stored dim differs
    # from the fake embedder's dim.
    row = CandidateRow(
        chunk_id=uuid4(),
        document_id=uuid4(),
        document_title="Doc",
        page_id=uuid4(),
        page_number=0,
        chunk_index=0,
        section_path=[],
        text="x",
        vector=[1.0, 0.0, 0.0],
        embedding_model_name="old-model",
        embedding_dim=3,
    )
    _set_candidates(monkeypatch, [row])
    # Scope must be non-empty so the pipeline reaches the dim check
    # rather than short-circuiting with NO_DOCUMENTS_READY.
    _set_list_ready(monkeypatch, [row.document_id])
    monkeypatch.setattr(
        sync_mod, "run_in_sync_session",
        lambda fn, *a, **kw: fn(None, *a, **kw),
    )

    embedder = _FakeEmbeddingModel(dim=4)  # 4 != 3
    llm = _FakeLLMProvider(tokens=["unused"])

    events: list[AnalysisEvent] = []
    async for ev in analyse_query(
        db_async=_FakeAsyncSession(),
        owner_id=uuid4(),
        question="q",
        document_ids=None,
        stream_id=uuid4(),
        embedding_model=embedder,
        llm_provider=llm,
    ):
        events.append(ev)
    assert isinstance(events[-1], AnalysisErrorEvent)
    assert events[-1].code == "PROVIDER_MISMATCH"
    assert llm.call_messages == []


@pytest.mark.asyncio
async def test_happy_path_yields_tokens_then_done(monkeypatch) -> None:
    import app.db.sync_session as sync_mod

    rows = [
        _candidate(title="Parallel", vector=[1.0, 0.0, 0.0, 0.0]),
        _candidate(title="Off-axis", vector=[0.0, 1.0, 0.0, 0.0]),
        _candidate(title="Anti", vector=[-1.0, 0.0, 0.0, 0.0]),
    ]
    # When `document_ids` is None, the orchestrator resolves scope via
    # `list_ready_documents`. Stub it to return at least one id so the
    # pipeline doesn't short-circuit with NO_DOCUMENTS_READY.
    fake_doc_id = uuid4()
    _set_candidates(monkeypatch, rows)
    _set_list_ready(monkeypatch, [fake_doc_id])
    monkeypatch.setattr(
        sync_mod, "run_in_sync_session",
        lambda fn, *a, **kw: fn(None, *a, **kw),
    )

    embedder = _FakeEmbeddingModel(dim=4, vector=[1.0, 0.0, 0.0, 0.0])
    llm = _FakeLLMProvider(tokens=["Parallel", " is", " top", "."])

    events: list[AnalysisEvent] = []
    async for ev in analyse_query(
        db_async=_FakeAsyncSession(),
        owner_id=uuid4(),
        question="Which is most parallel?",
        document_ids=None,
        stream_id=uuid4(),
        embedding_model=embedder,
        llm_provider=llm,
    ):
        events.append(ev)

    token_events = [e for e in events if isinstance(e, AnalysisTokenEvent)]
    assert [t.text for t in token_events] == ["Parallel", " is", " top", "."]

    done = events[-1]
    assert isinstance(done, AnalysisDoneEvent)
    assert done.answer == "Parallel is top."
    assert done.citations and len(done.citations) >= 1
    assert done.citations[0]["document_title"] == "Parallel"
    assert done.citations[0]["index"] == 1

    assert len(llm.call_messages) == 1
    msgs = llm.call_messages[0]
    assert msgs[0].role == "system"
    assert msgs[1].role == "user"
    assert "[1]" in msgs[1].content
    assert "Parallel" in msgs[1].content


@pytest.mark.asyncio
async def test_analyse_query_once_returns_query_response_on_success(monkeypatch) -> None:
    import app.db.sync_session as sync_mod

    rows = [_candidate(title="Solo", vector=[1.0, 0.0, 0.0, 0.0])]
    _set_candidates(monkeypatch, rows)
    _set_list_ready(monkeypatch, [uuid4()])
    monkeypatch.setattr(
        sync_mod, "run_in_sync_session",
        lambda fn, *a, **kw: fn(None, *a, **kw),
    )

    embedder = _FakeEmbeddingModel(dim=4, vector=[1.0, 0.0, 0.0, 0.0])
    llm = _FakeLLMProvider(tokens=["hi"])

    outcome = await analyse_query_once(
        db_async=_FakeAsyncSession(),
        owner_id=uuid4(),
        question="q",
        document_ids=None,
        stream_id=uuid4(),
        llm_provider=llm,
        embedding_model=embedder,
    )
    assert outcome.response is not None
    assert outcome.response.answer == "hi"
    assert outcome.response.citations and len(outcome.response.citations) >= 1


@pytest.mark.asyncio
async def test_analyse_query_once_returns_error_on_failure(monkeypatch) -> None:
    """When the orchestrator yields an error, `analyse_query_once`
    surfaces a `_ErrorOutcome` with the matching code."""
    import app.db.sync_session as sync_mod

    _set_candidates(monkeypatch, [])
    _set_list_ready(monkeypatch, [])
    monkeypatch.setattr(
        sync_mod, "run_in_sync_session",
        lambda fn, *a, **kw: fn(None, *a, **kw),
    )

    embedder = _FakeEmbeddingModel(dim=4)
    llm = _FakeLLMProvider(tokens=["unused"])

    outcome = await analyse_query_once(
        db_async=_FakeAsyncSession(),
        owner_id=uuid4(),
        question="q",
        document_ids=None,
        stream_id=uuid4(),
        llm_provider=llm,
        embedding_model=embedder,
    )
    assert getattr(outcome, "response", None) is None
    assert getattr(outcome, "code", None) == "NO_DOCUMENTS_READY"
