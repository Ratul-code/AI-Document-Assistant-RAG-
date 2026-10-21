"""Tests for the post-retrieval rerank stage (ADR-019).

These tests exercise the orchestrator's rerank wiring without loading
a real cross-encoder. A stub ``Reranker`` (declared inline) replaces
``get_default_reranker`` so the test can assert on which rows were
re-ordered, that the citation payload carries the new fields, and
that timeouts / exceptions fall back gracefully.

Pipeline under test:
    dense/BM25 → RRF (or cosine-only) top-K → rerank → final top-K.

We drive the orchestrator with the same helpers
(``_set_candidates``, ``_set_list_ready``, ``run_in_sync_session``
patch) used by ``test_end_to_end.py`` so the rerank test reads as a
thin extension of the happy-path test, not a parallel structure.
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
# Fakes
# ---------------------------------------------------------------------


class _StubReranker:
    """Deterministic reranker that reverses the input order.

    Scores descending by `len(documents) - i` so the LAST input row
    wins with the highest score. This is deliberately the opposite of
    the retriever's ordering (which puts the parallel vector first) so
    the test can prove the reranker — not the retriever — decided the
    final top-K.
    """

    def __init__(self) -> None:
        self.name = "stub:reverse"
        self.calls: list[tuple[str, list[str]]] = []

    def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_k: int,
    ) -> list[tuple[int, float]]:
        self.calls.append((query, list(documents)))
        if not documents:
            return []
        n = min(top_k, len(documents))
        return [(len(documents) - 1 - i, float(n - i)) for i in range(n)]


class _TimeoutReranker:
    """Reranker that always raises, so the timeout fallback is exercised."""

    name = "stub:timeout"

    def rerank(self, query, documents, *, top_k):
        raise RuntimeError("simulated reranker crash")


class _FakeEmbeddingModel:
    def __init__(self, *, dim: int = 4, vector: list[float] | None = None) -> None:
        self.dim = dim
        self.model_name = "fake-embed"
        self._vector = vector or [1.0, 0.0, 0.0, 0.0][:dim]

    def embed(self, text: str):  # returns an EmbeddingResult-shaped object
        from app.services.ai.embeddings import EmbeddingResult
        return EmbeddingResult(
            vector=list(self._vector), dim=self.dim, model_name=self.model_name,
        )

    def embed_batch(self, texts: list[str]):
        return [self.embed(t) for t in texts]


class _FakeLLMProvider:
    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens
        self.call_messages: list = []

    async def stream(self, messages) -> AsyncIterator[str]:
        self.call_messages.append(messages)
        for t in self._tokens:
            yield t

    async def complete(self, messages) -> str:
        self.call_messages.append(messages)
        return "".join(self._tokens)


class _FakeAsyncSession:
    pass


def _candidate(
    *, title: str, vector: list[float] | None = None, text: str | None = None
):
    from app.repositories.embedding_search import CandidateRow
    return CandidateRow(
        chunk_id=uuid4(),
        document_id=uuid4(),
        document_title=title,
        page_id=uuid4(),
        page_number=0,
        chunk_index=0,
        section_path=[],
        text=text if text is not None else f"{title} body for testing.",
        vector=vector or [0.0] * 4,
        embedding_model_name="fake-embed",
        embedding_dim=4,
    )


def _set_candidates(monkeypatch, rows):
    import app.services.document.analyser as analyser_pkg

    def _f(db, *, owner_id, document_ids=None):  # noqa: ARG001
        return list(rows)

    monkeypatch.setattr(analyser_pkg, "fetch_candidate_rows", _f)


def _set_list_ready(monkeypatch, value=()):
    import app.repositories.document as document_mod

    rows = [v if hasattr(v, "id") else SimpleNamespace(id=v) for v in value]

    async def _f(db, *, owner_id):  # noqa: ARG001
        return list(rows)

    monkeypatch.setattr(document_mod, "list_ready_documents", _f)


def _patch_sync_session(monkeypatch):
    import app.db.sync_session as sync_mod
    monkeypatch.setattr(
        sync_mod, "run_in_sync_session",
        lambda fn, *a, **kw: fn(None, *a, **kw),
    )


# ---------------------------------------------------------------------
# Rerank = ON
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rerank_enabled_reorders_and_badge_citations(monkeypatch) -> None:
    """When the reranker is on, the final top-K reflects the
    reranker's ordering, not the retriever's. The citation payload
    carries `rerank=True` and `original_score` for every entry."""
    from app.services.document.analyser import (
        AnalysisDoneEvent,
        AnalysisErrorEvent,
        analyse_query,
    )
    from app.services.ai.rerankers import get_default_reranker

    # conftest sets ANALYSIS_RERANK_ENABLED=false for the whole suite;
    # enable it for this test (the reranker is stubbed via monkeypatch
    # below, so we don't need sentence_transformers installed).
    monkeypatch.setenv("ANALYSIS_RERANK_ENABLED", "true")

    _set_candidates(
        monkeypatch,
        [
            _candidate(title="Parallel", vector=[1.0, 0.0, 0.0, 0.0]),
            _candidate(title="Anti", vector=[-1.0, 0.0, 0.0, 0.0]),
            _candidate(title="Off-axis", vector=[0.0, 1.0, 0.0, 0.0]),
        ],
    )
    _set_list_ready(monkeypatch, [uuid4()])
    _patch_sync_session(monkeypatch)

    monkeypatch.setenv("ANALYSIS_RERANK_ENABLED", "true")
    monkeypatch.setenv("ANALYSIS_RETRIEVAL_MODE", "dense")  # simpler retriever
    monkeypatch.setenv("ANALYSIS_TOP_K", "2")
    monkeypatch.setenv("ANALYSIS_RERANK_TOP_K", "2")

    embedder = _FakeEmbeddingModel(dim=4, vector=[1.0, 0.0, 0.0, 0.0])
    llm = _FakeLLMProvider(tokens=["ok"])
    stub = _StubReranker()
    monkeypatch.setattr(
        "app.services.document.analyser.get_default_reranker", lambda: stub,
    )
    # Avoid any factory singleton carryover from a previous test.
    get_default_reranker  # referenced import; reset handled by autouse fixture

    events = []
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

    # No error events.
    assert not any(isinstance(e, AnalysisErrorEvent) for e in events)
    done = events[-1]
    assert isinstance(done, AnalysisDoneEvent)
    assert done.citations and len(done.citations) == 2

    # The stub reranker reverses the input pool, so the final
    # citations are the LAST two rows of the retriever's pool, in
    # reverse order. We assert the *property* (reversal) rather than
    # specific titles because the retriever's secondary-key tiebreak
    # is non-obvious for the two zero-cosine rows.
    titles = [c["document_title"] for c in done.citations]
    assert len(titles) == 2
    # `Parallel` was cosine=1.0 and is therefore in the retriever's
    # top-1; after reversal it should NOT be in the final top-2
    # (unless the pool is small, which it isn't here — pool is 3).
    assert "Parallel" not in titles

    # Citation payload carries the new fields.
    for c in done.citations:
        assert c["rerank"] is True
        assert c["rerank_provider"] == "stub:reverse"
        # original_score is the retriever's cosine score (Parallel=1.0,
        # Anti=-1.0, Off-axis=0.0). After the reranker's reversal, the
        # surviving entries are Off-axis (0.0) and Anti (-1.0).
        assert c["original_score"] is not None
        # `score` is now the reranker's score, not the retriever's.
        assert c["score"] != c["original_score"]


@pytest.mark.asyncio
async def test_rerank_disabled_citations_have_no_rerank_badge(monkeypatch) -> None:
    """When rerank is OFF (the default), the citation payload must
    look identical to the pre-rerank contract — `rerank=False`,
    `original_score=None`, `rerank_provider=None`. Backwards-
    compatibility guarantee for clients."""
    from app.services.document.analyser import (
        AnalysisDoneEvent,
        analyse_query,
    )

    _set_candidates(
        monkeypatch,
        [
            _candidate(title="Parallel", vector=[1.0, 0.0, 0.0, 0.0]),
            _candidate(title="Off-axis", vector=[0.0, 1.0, 0.0, 0.0]),
        ],
    )
    _set_list_ready(monkeypatch, [uuid4()])
    _patch_sync_session(monkeypatch)

    monkeypatch.delenv("ANALYSIS_RERANK_ENABLED", raising=False)
    monkeypatch.setenv("ANALYSIS_RETRIEVAL_MODE", "dense")
    monkeypatch.setenv("ANALYSIS_TOP_K", "2")

    embedder = _FakeEmbeddingModel(dim=4, vector=[1.0, 0.0, 0.0, 0.0])
    llm = _FakeLLMProvider(tokens=["ok"])

    events = []
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

    done = events[-1]
    assert isinstance(done, AnalysisDoneEvent)
    assert done.citations
    for c in done.citations:
        assert c["rerank"] is False
        assert c["original_score"] is None
        assert c["rerank_provider"] is None
        # Retriever's order: Parallel then Off-axis.
    assert done.citations[0]["document_title"] == "Parallel"


@pytest.mark.asyncio
async def test_rerank_timeout_falls_back_to_pre_rerank_order(monkeypatch) -> None:
    """If the reranker raises, the orchestrator must NOT fail the
    query — it falls back to the retriever's order and the citations
    look as if rerank never ran."""
    from app.services.document.analyser import (
        AnalysisDoneEvent,
        AnalysisErrorEvent,
        analyse_query,
    )

    # conftest sets rerank off; opt in for this test.
    monkeypatch.setenv("ANALYSIS_RERANK_ENABLED", "true")

    _set_candidates(
        monkeypatch,
        [
            _candidate(title="Parallel", vector=[1.0, 0.0, 0.0, 0.0]),
            _candidate(title="Off-axis", vector=[0.0, 1.0, 0.0, 0.0]),
        ],
    )
    _set_list_ready(monkeypatch, [uuid4()])
    _patch_sync_session(monkeypatch)

    monkeypatch.setenv("ANALYSIS_RERANK_ENABLED", "true")
    monkeypatch.setenv("ANALYSIS_RETRIEVAL_MODE", "dense")
    monkeypatch.setenv("ANALYSIS_TOP_K", "2")
    monkeypatch.setenv("ANALYSIS_RERANK_TOP_K", "2")
    # The minimum valid value (0.1) — short enough that the timeout
    # fires well before any real model finishes, but within the
    # schema's ge=0.1 constraint. The stub raises instantly anyway,
    # so the timeout isn't actually what we depend on here.
    monkeypatch.setenv("ANALYSIS_RERANK_TIMEOUT_S", "0.1")

    embedder = _FakeEmbeddingModel(dim=4, vector=[1.0, 0.0, 0.0, 0.0])
    llm = _FakeLLMProvider(tokens=["ok"])
    monkeypatch.setattr(
        "app.services.document.analyser.get_default_reranker",
        lambda: _TimeoutReranker(),
    )

    events = []
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

    assert not any(isinstance(e, AnalysisErrorEvent) for e in events)
    done = events[-1]
    assert isinstance(done, AnalysisDoneEvent)
    # Fallback path: the retriever's order survives. Parallel (cosine
    # 1.0) is #1; Off-axis (cosine 0.0) beats Anti (cosine -1.0) so
    # it's #2.
    titles = [c["document_title"] for c in done.citations]
    assert titles[0] == "Parallel"
    for c in done.citations:
        assert c["rerank"] is False
        assert c["original_score"] is None


# ---------------------------------------------------------------------
# Synchronous rerank_top_rows helper — pure function tests
# ---------------------------------------------------------------------


def test_rerank_top_rows_with_none_reranker_is_identity() -> None:
    from app.services.ai.rerankers import NoneReranker
    from app.services.document.analyser import rerank_top_rows

    rows = [
        _candidate(title="A", text="alpha"),
        _candidate(title="B", text="beta"),
        _candidate(title="C", text="gamma"),
    ]
    ranked = [(r, float(i)) for i, r in enumerate(rows)]
    result = rerank_top_rows(
        ranked, "q", reranker=NoneReranker(), top_k=2,
    )
    # Identity: original order, original scores.
    assert [r.document_title for r, _ in result.top_rows] == ["A", "B"]
    assert [s for _, s in result.top_rows] == [0.0, 1.0]
    assert result.provider is None
    assert result.original_scores is None


def test_rerank_top_rows_reorders_and_returns_original_scores() -> None:
    from app.services.document.analyser import rerank_top_rows

    rows = [
        _candidate(title="A", text="alpha"),
        _candidate(title="B", text="beta"),
        _candidate(title="C", text="gamma"),
    ]
    # Retriever scores: A=0.9, B=0.5, C=0.1.
    ranked = [(rows[0], 0.9), (rows[1], 0.5), (rows[2], 0.1)]

    # Stub reranker reverses the pool. Scores it returns are
    # descending by reverse-index, so the LAST pool row gets the
    # highest score and is picked at top_k=1.
    class _Reverse:
        name = "stub:reverse"

        def rerank(self, query, documents, *, top_k):
            n = min(top_k, len(documents))
            return [(len(documents) - 1 - i, float(n - i)) for i in range(n)]

    result = rerank_top_rows(
        ranked, "q", reranker=_Reverse(), top_k=2, pool_multiplier=3,
    )
    # Reranker with top_k=2 returns 2 rows: C first (index 2), B
    # second (index 1).
    assert [r.document_title for r, _ in result.top_rows] == ["C", "B"]
    assert result.provider == "stub:reverse"
    # original_scores are parallel to top_rows in reranker order:
    # C originally had 0.1, B originally had 0.5.
    assert result.original_scores == pytest.approx([0.1, 0.5])
    # Reranker-decided scores from the stub are (n, n-1) for n=2.
    assert [s for _, s in result.top_rows] == pytest.approx([2.0, 1.0])


def test_rerank_top_rows_respects_pool_multiplier() -> None:
    """With pool_multiplier=1, the pool is just `ranked[:top_k]`.
    Reranking cannot surface anything outside that pool."""
    from app.services.document.analyser import rerank_top_rows

    rows = [_candidate(title=t, text=t) for t in ["A", "B", "C", "D", "E"]]
    ranked = [(r, float(i)) for i, r in enumerate(rows)]

    captured_docs: list[str] = []

    class _Spy:
        name = "spy"

        def rerank(self, query, documents, *, top_k):
            captured_docs.extend(documents)
            n = min(top_k, len(documents))
            return [(i, float(n - i)) for i in range(n)]

    result = rerank_top_rows(
        ranked, "q", reranker=_Spy(), top_k=2, pool_multiplier=1,
    )
    # Pool is just `ranked[:2]` → A and B. C, D, E never reach the
    # reranker.
    assert captured_docs == ["A", "B"]
    assert [r.document_title for r, _ in result.top_rows] == ["A", "B"]


def test_rerank_top_rows_empty_input() -> None:
    from app.services.document.analyser import rerank_top_rows

    result = rerank_top_rows([], "q", reranker=_StubReranker(), top_k=5)
    assert result.top_rows == []
    assert result.original_scores is None
    assert result.provider is None
