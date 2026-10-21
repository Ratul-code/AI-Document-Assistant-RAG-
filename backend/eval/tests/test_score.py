"""Tests for the pure scoring primitives.

These exercise `recall_at_k`, `mrr`, and `aggregate` against
hand-crafted retrieval orderings. No app imports, no DB, no
embedder — these are pure functions on Python lists/sets.
"""

from __future__ import annotations

from uuid import UUID

from eval.scripts.score import (
    PerQuestionResult,
    aggregate,
    mrr,
    recall_at_k,
)


# Stable UUIDs for tests (deterministic string compares).
A = UUID("11111111-1111-1111-1111-111111111111")
B = UUID("22222222-2222-2222-2222-222222222222")
C = UUID("33333333-3333-3333-3333-333333333333")
D = UUID("44444444-4444-4444-4444-444444444444")


def test_recall_perfect_all_top_k() -> None:
    # All 3 relevant chunks retrieved in top 3.
    relevant = {A, B, C}
    retrieved = [A, B, C, D]
    assert recall_at_k(retrieved, relevant, k=3) == 1.0
    assert recall_at_k(retrieved, relevant, k=5) == 1.0


def test_recall_zero_no_overlap() -> None:
    relevant = {A, B}
    retrieved = [C, D]
    assert recall_at_k(retrieved, relevant, k=5) == 0.0


def test_recall_partial_some_in_top_k() -> None:
    # 1 of 2 relevant chunks appears in position 2.
    relevant = {A, B}
    retrieved = [C, A]
    assert recall_at_k(retrieved, relevant, k=1) == 0.0
    assert recall_at_k(retrieved, relevant, k=2) == 0.5
    assert recall_at_k(retrieved, relevant, k=10) == 0.5


def test_recall_at_k_zero_k_returns_zero() -> None:
    # k=0 is an invalid sentinel — the evaluator never calls it, but
    # the function should not divide by zero.
    assert recall_at_k([A, B], {A}, k=0) == 0.0


def test_recall_at_k_empty_retrieved_returns_zero() -> None:
    assert recall_at_k([], {A, B}, k=5) == 0.0


def test_recall_at_k_empty_relevant_returns_zero() -> None:
    # Defensive: if no gold relevant chunks, recall is undefined.
    # Returning 0.0 is the most useful sentinel (won't pollute the
    # average with NaNs).
    assert recall_at_k([A, B], set(), k=5) == 0.0


def test_mrr_first_hit_is_one() -> None:
    # First retrieved is relevant -> MRR = 1/1 = 1.0.
    assert mrr([A], {A}) == 1.0


def test_mrr_second_hit_is_one_half() -> None:
    assert mrr([B, A], {A}) == 0.5


def test_mrr_third_hit_is_one_third() -> None:
    assert mrr([B, C, A], {A}) == 1.0 / 3.0


def test_mrr_no_hit_is_zero() -> None:
    assert mrr([B, C, D], {A}) == 0.0


def test_mrr_empty_retrieved_is_zero() -> None:
    assert mrr([], {A}) == 0.0


def test_mrr_empty_relevant_is_zero() -> None:
    assert mrr([A, B], set()) == 0.0


def test_mrr_uses_first_relevant_only() -> None:
    # Two relevant hits in the list; only the first one counts for MRR.
    assert mrr([A, B], {A, B}) == 1.0


def test_aggregate_averages_per_question_metrics() -> None:
    per_q = [
        PerQuestionResult(
            question_id="q1",
            retrieved_ids=[A, B],
            relevant_ids=[A],
            recall_at_1=1.0,
            recall_at_5=1.0,
            recall_at_10=1.0,
            reciprocal_rank=1.0,
        ),
        PerQuestionResult(
            question_id="q2",
            retrieved_ids=[C, D, A],
            relevant_ids=[A],
            recall_at_1=0.0,
            recall_at_5=0.0,
            recall_at_10=1.0,
            reciprocal_rank=1.0 / 3.0,
        ),
        PerQuestionResult(
            question_id="q3",
            retrieved_ids=[C],
            relevant_ids=[A],
            recall_at_1=0.0,
            recall_at_5=0.0,
            recall_at_10=0.0,
            reciprocal_rank=0.0,
        ),
    ]
    metrics = aggregate(per_q)
    assert metrics.recall_at_1 == 1.0 / 3.0
    assert metrics.recall_at_5 == 1.0 / 3.0
    assert metrics.recall_at_10 == 2.0 / 3.0
    # MRR = (1.0 + 0.333... + 0.0) / 3 ≈ 0.4444
    assert abs(metrics.mrr - (1.0 + 1.0 / 3.0) / 3.0) < 1e-9
    assert metrics.questions_total == 3
    assert metrics.questions_scored == 3


def test_aggregate_empty_input_returns_zeros() -> None:
    metrics = aggregate([])
    assert metrics.recall_at_1 == 0.0
    assert metrics.recall_at_5 == 0.0
    assert metrics.recall_at_10 == 0.0
    assert metrics.mrr == 0.0
    assert metrics.questions_total == 0
    assert metrics.questions_scored == 0