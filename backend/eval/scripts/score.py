"""Pure scoring primitives for the retrieval eval.

Three responsibilities:
1. `recall_at_k` — fraction of relevant ids that appear in the top-k
   of a retrieved ordering.
2. `mrr` — reciprocal rank of the first relevant hit (1.0, 0.5, 0.333,
   ..., 0.0 if no relevant hit).
3. `aggregate` — averages the per-question metrics across a dataset.

These are pure functions on Python lists/sets. No app imports, no DB,
no embedder. They are exercised directly by `tests/test_score.py`.

The runner (`scripts/run_retrieval_eval.py`) is the only caller that
needs `PerQuestionResult` and `AggregatedMetrics`; the per-question
pydantic model serialises into the JSON result file and the markdown
report builder reads the aggregated metrics.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class PerQuestionResult:
    """One question's retrieval outcome.

    `retrieved_ids` and `relevant_ids` are kept as UUIDs in-process for
    deterministic set comparison; they are serialised to strings when
    writing JSON.
    """

    question_id: str
    retrieved_ids: list[UUID]
    relevant_ids: list[UUID]
    recall_at_1: float
    recall_at_5: float
    recall_at_10: float
    reciprocal_rank: float


@dataclass(frozen=True)
class AggregatedMetrics:
    """Averaged retrieval metrics across all questions in a dataset."""

    recall_at_1: float
    recall_at_5: float
    recall_at_10: float
    mrr: float
    questions_total: int
    questions_scored: int


def recall_at_k(
    retrieved: Sequence[UUID],
    relevant: Iterable[UUID],
    k: int,
) -> float:
    """Fraction of `relevant` that appears in the first `k` of `retrieved`.

    Edge cases:
    *  `k <= 0` → returns 0.0 (the caller should never pass it).
    *  `relevant` empty → returns 0.0 (recall undefined; we return
       0.0 to avoid NaN pollution of the per-dataset average).
    *  `retrieved` empty → returns 0.0.
    """
    if k <= 0:
        return 0.0
    relevant_set = set(relevant)
    if not relevant_set:
        return 0.0
    top_k = list(retrieved[:k])
    if not top_k:
        return 0.0
    hits = sum(1 for cid in top_k if cid in relevant_set)
    return hits / len(relevant_set)


def mrr(
    retrieved: Sequence[UUID],
    relevant: Iterable[UUID],
) -> float:
    """Reciprocal rank of the first relevant hit in `retrieved`.

    Returns `1.0 / rank` where `rank` is the 1-based position of the
    first relevant id in `retrieved`. Returns 0.0 if no relevant hit
    appears or if either argument is empty.
    """
    relevant_set = set(relevant)
    if not relevant_set:
        return 0.0
    for idx, cid in enumerate(retrieved, start=1):
        if cid in relevant_set:
            return 1.0 / idx
    return 0.0


def aggregate(per_question: Sequence[PerQuestionResult]) -> AggregatedMetrics:
    """Mean of the per-question metrics, with question counts for audit.

    `questions_scored` is the number of `PerQuestionResult` rows fed in
    (== `len(per_question)`). `questions_total` is the same — the
    caller (the runner) tracks the *input* count separately if it
    wants to report "scored N of M total".
    """
    total = len(per_question)
    if total == 0:
        return AggregatedMetrics(
            recall_at_1=0.0,
            recall_at_5=0.0,
            recall_at_10=0.0,
            mrr=0.0,
            questions_total=0,
            questions_scored=0,
        )
    return AggregatedMetrics(
        recall_at_1=sum(r.recall_at_1 for r in per_question) / total,
        recall_at_5=sum(r.recall_at_5 for r in per_question) / total,
        recall_at_10=sum(r.recall_at_10 for r in per_question) / total,
        mrr=sum(r.reciprocal_rank for r in per_question) / total,
        questions_total=total,
        questions_scored=total,
    )


__all__ = [
    "AggregatedMetrics",
    "PerQuestionResult",
    "aggregate",
    "mrr",
    "recall_at_k",
]
