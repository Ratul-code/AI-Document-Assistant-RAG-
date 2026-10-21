"""Tests for the pure NumPy top-K cosine ranking.

These tests don't touch the DB — they exercise `rank_top_rows`
directly with synthetic `CandidateRow`s.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.repositories.embedding_search import CandidateRow
from app.services.document.analyser import rank_top_rows


def _row(
    *,
    title: str,
    vector: list[float],
    page_number: int = 0,
    chunk_index: int = 0,
    section_path: list[str] | None = None,
    text: str = "",
) -> CandidateRow:
    return CandidateRow(
        chunk_id=uuid4(),
        document_id=uuid4(),
        document_title=title,
        page_id=uuid4(),
        page_number=page_number,
        chunk_index=chunk_index,
        section_path=section_path or [],
        text=text,
        vector=vector,
        embedding_model_name="test",
        embedding_dim=len(vector),
    )


# ---------------------------------------------------------------------
# Basic ranking
# ---------------------------------------------------------------------


def test_empty_input_returns_empty() -> None:
    assert rank_top_rows([], [1.0, 0.0], top_k=5) == []


def test_top_k_zero_returns_empty() -> None:
    rows = [_row(title="x", vector=[1.0, 0.0])]
    assert rank_top_rows(rows, [1.0, 0.0], top_k=0) == []


def test_single_match_ranks_correctly() -> None:
    rows = [_row(title="A", vector=[1.0, 0.0])]
    q = [1.0, 0.0]
    [(row, score)] = rank_top_rows(rows, q, top_k=5)
    assert row.document_title == "A"
    assert score == pytest.approx(1.0)


def test_descending_order_by_cosine() -> None:
    rows = [
        _row(title="parallel", vector=[1.0, 0.0]),
        _row(title="orthogonal", vector=[0.0, 1.0]),
        _row(title="antiparallel", vector=[-1.0, 0.0]),
        _row(title="thirty", vector=[0.866, 0.5]),  # 30 deg
    ]
    q = [1.0, 0.0]
    ranked = rank_top_rows(rows, q, top_k=4)
    titles = [r.document_title for r, _ in ranked]
    assert titles == ["parallel", "thirty", "orthogonal", "antiparallel"]


def test_top_k_truncates_results() -> None:
    rows = [_row(title=str(i), vector=[float(i), 1.0 - i / 10.0]) for i in range(1, 11)]
    q = [1.0, 0.0]
    ranked = rank_top_rows(rows, q, top_k=3)
    assert len(ranked) == 3


# ---------------------------------------------------------------------
# Threshold gate
# ---------------------------------------------------------------------


def test_min_cosine_filters_negative_scores() -> None:
    rows = [
        _row(title="positive", vector=[1.0, 0.0]),
        _row(title="zero", vector=[0.0, 1.0]),
        _row(title="negative", vector=[-1.0, 0.0]),
    ]
    q = [1.0, 0.0]
    ranked = rank_top_rows(rows, q, top_k=5, min_cosine=0.1)
    titles = [r.document_title for r, _ in ranked]
    assert titles == ["positive"]


def test_min_cosine_zero_keeps_all() -> None:
    rows = [
        _row(title="positive", vector=[1.0, 0.0]),
        _row(title="negative", vector=[-1.0, 0.0]),
    ]
    q = [1.0, 0.0]
    ranked = rank_top_rows(rows, q, top_k=5, min_cosine=0.0)
    assert len(ranked) == 2


# ---------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------


def test_zero_query_vector_returns_empty() -> None:
    rows = [_row(title="A", vector=[1.0, 0.0])]
    assert rank_top_rows(rows, [0.0, 0.0], top_k=5) == []


def test_zero_row_vector_scores_zero_not_crash() -> None:
    rows = [
        _row(title="zero", vector=[0.0, 0.0]),
        _row(title="normal", vector=[1.0, 0.0]),
    ]
    q = [1.0, 0.0]
    ranked = rank_top_rows(rows, q, top_k=5)
    titles = [r.document_title for r, _ in ranked]
    assert titles == ["normal", "zero"]
    # zero-row cosine is 0.0 (not NaN)
    zero_score = next(s for r, s in ranked if r.document_title == "zero")
    assert zero_score == 0.0


def test_determinism_secondary_sort() -> None:
    """Same score + same document_id → secondary key is chunk_index ASC."""
    # Build all three rows with the SAME document_id so the secondary
    # sort hits chunk_index.
    from uuid import uuid4
    same_doc_id = uuid4()
    rows = []
    for i in range(3):
        rows.append(
            CandidateRow(
                chunk_id=uuid4(),
                document_id=same_doc_id,
                document_title=f"eq-{i}",
                page_id=uuid4(),
                page_number=0,
                chunk_index=i,
                section_path=[],
                text="x",
                vector=[1.0, 1.0],
                embedding_model_name="test",
                embedding_dim=2,
            )
        )
    q = [1.0, 1.0]
    ranked = rank_top_rows(rows, q, top_k=3)
    scores = [s for _, s in ranked]
    assert all(abs(s - scores[0]) < 1e-6 for s in scores)
    chunk_indices = [r.chunk_index for r, _ in ranked]
    assert chunk_indices == [0, 1, 2]


def test_normalisation_handles_different_lengths() -> None:
    """Long and short vectors should produce the same cosine."""
    rows = [
        _row(title="short", vector=[1.0, 0.0]),
        _row(title="long", vector=[3.0, 0.0]),
    ]
    q_short = [1.0, 0.0]
    q_long = [5.0, 0.0]
    short_ranked = rank_top_rows(rows, q_short, top_k=2)
    long_ranked = rank_top_rows(rows, q_long, top_k=2)
    assert short_ranked[0][1] == pytest.approx(1.0)
    assert long_ranked[0][1] == pytest.approx(1.0)
