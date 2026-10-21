"""Tests for `eval/scripts/run_retrieval_eval.py`.

Today this is a single regression test for the rerank-eval helper.
The helper previously destructured `result.top_rows` as if it were
``[(int_idx, score)]`` when it was ``[(CandidateRow, float)]``,
producing::

    TypeError: list indices must be integers or slices, not CandidateRow

The bug was caught in the live eval with rerank on; this test pins the
fixed shape so it can't regress.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


def _make_row(*, title: str, text: str):
    """Build a minimal CandidateRow-shaped object for the eval helper."""
    from app.repositories.embedding_search import CandidateRow
    return CandidateRow(
        chunk_id=uuid4(),
        document_id=uuid4(),
        document_title=title,
        page_id=uuid4(),
        page_number=0,
        chunk_index=0,
        section_path=[],
        text=text,
        vector=[],
        embedding_model_name="fake-embed",
        embedding_dim=4,
    )


class _ReverseReranker:
    """Stub reranker that reverses the input pool.

    Mirrors the contract `rerank_top_rows` expects:
    `rerank(query, documents, *, top_k)` -> `[(int_idx, float), ...]`.
    """

    name = "stub:reverse"

    def rerank(self, query, documents, *, top_k):
        if not documents:
            return []
        n = min(top_k, len(documents))
        return [(len(documents) - 1 - i, float(n - i)) for i in range(n)]


def test_rerank_chunk_ids_returns_correct_chunk_ids_in_reranker_order(monkeypatch):
    """The eval helper must hand back chunk_ids in the reranker's
    order, not the retriever's. This is the property the prior bug
    (TypeError on `pool_rows[CandidateRow]`) prevented us from
    verifying — the warning-and-fallback path masked it in CI."""
    from eval.scripts.run_retrieval_eval import _rerank_chunk_ids

    # Stub out the production reranker factory on BOTH sides — the
    # helper imports it lazily from `app.services.ai.rerankers` and
    # also calls it via the analyser module's reference, so we have
    # to patch both to avoid instantiating a real CrossEncoder.
    import app.services.ai.rerankers as rerankers_pkg
    monkeypatch.setattr(
        rerankers_pkg, "_reranker_singleton", _ReverseReranker(),
    )

    row_a = _make_row(title="A", text="alpha")
    row_b = _make_row(title="B", text="beta")
    row_c = _make_row(title="C", text="gamma")

    # Retriever's order: A, B, C. The stub reverses, so we expect
    # C, B back (top_k=2 out of a 3-doc pool).
    ranked_chunk_ids = [row_a.chunk_id, row_b.chunk_id, row_c.chunk_id]
    lookup = {row.chunk_id: row for row in (row_a, row_b, row_c)}

    result = _rerank_chunk_ids(
        ranked_chunk_ids=ranked_chunk_ids,
        question="q",
        rerank_top_k=2,
        pool_multiplier=3,
        timeout_s=5.0,
        candidate_lookup=lookup.get,
    )

    # Should be chunk_ids in reranker order: C, B (not A).
    assert [str(c) for c in result] == [str(row_c.chunk_id), str(row_b.chunk_id)]


def test_rerank_chunk_ids_falls_back_on_reranker_failure(monkeypatch):
    """If the reranker itself raises, the helper must NOT crash the
    eval — it must return the pre-rerank order (mirroring the
    production `rerank_top_rows_async` policy)."""
    from eval.scripts.run_retrieval_eval import _rerank_chunk_ids

    class _BoomReranker:
        name = "stub:boom"

        def rerank(self, query, documents, *, top_k):
            raise RuntimeError("simulated reranker crash")

    import app.services.ai.rerankers as rerankers_pkg
    monkeypatch.setattr(
        rerankers_pkg, "_reranker_singleton", _BoomReranker(),
    )

    row_a = _make_row(title="A", text="alpha")
    row_b = _make_row(title="B", text="beta")
    row_c = _make_row(title="C", text="gamma")
    ranked_chunk_ids = [row_a.chunk_id, row_b.chunk_id, row_c.chunk_id]
    lookup = {row.chunk_id: row for row in (row_a, row_b, row_c)}

    # rerank_top_k=2 → fallback returns first 2 of the input.
    result = _rerank_chunk_ids(
        ranked_chunk_ids=ranked_chunk_ids,
        question="q",
        rerank_top_k=2,
        pool_multiplier=3,
        timeout_s=5.0,
        candidate_lookup=lookup.get,
    )

    assert [str(c) for c in result] == [str(row_a.chunk_id), str(row_b.chunk_id)]


def test_rerank_chunk_ids_falls_back_when_pool_lookup_misses(monkeypatch):
    """If any chunk in the pre-rerank pool is missing from the
    candidate lookup (e.g. stale corpus state), the helper must
    return the input slice rather than raise."""
    from eval.scripts.run_retrieval_eval import _rerank_chunk_ids

    import app.services.ai.rerankers as rerankers_pkg
    monkeypatch.setattr(
        rerankers_pkg, "_reranker_singleton", _ReverseReranker(),
    )

    row_a = _make_row(title="A", text="alpha")
    row_b = _make_row(title="B", text="beta")
    ranked_chunk_ids = [row_a.chunk_id, row_b.chunk_id]

    # Lookup deliberately omits row_b — the helper should bail out
    # on the `any(r is None for r in pool_rows)` guard.
    lookup = {row_a.chunk_id: row_a}  # row_b is missing

    result = _rerank_chunk_ids(
        ranked_chunk_ids=ranked_chunk_ids,
        question="q",
        rerank_top_k=1,
        pool_multiplier=3,
        timeout_s=5.0,
        candidate_lookup=lookup.get,
    )

    assert [str(c) for c in result] == [str(row_a.chunk_id)]


def test_rerank_chunk_ids_handles_empty_input(monkeypatch):
    """Empty input is a valid early-return — the helper should not
    crash and should return []."""
    from eval.scripts.run_retrieval_eval import _rerank_chunk_ids

    # The empty path still calls `get_default_reranker()` (the early
    # return on `pool_rows` only triggers when the pool is non-empty
    # and the lookup misses). Inject a stub so we don't need a real
    # CrossEncoder on disk.
    import app.services.ai.rerankers as rerankers_pkg
    monkeypatch.setattr(
        rerankers_pkg, "_reranker_singleton", _ReverseReranker(),
    )

    result = _rerank_chunk_ids(
        ranked_chunk_ids=[],
        question="q",
        rerank_top_k=5,
        pool_multiplier=3,
        timeout_s=5.0,
        candidate_lookup=lambda _: None,
    )
    assert result == []