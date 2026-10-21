"""Reciprocal Rank Fusion + the `hybrid_retrieve` orchestrator.

Pure functions over `CandidateRow`. Both are async-friendly only in
the sense that the caller can wrap them in `asyncio.to_thread`;
internally they are sync.

Reference: Cormack, Clarke, Buettcher, "Reciprocal Rank Fusion outperforms
Condorcet and individual Rank Learning Methods" (SIGIR 2009). The
score for a document d with ranks r_1, r_2, … across N input lists is:

    RRF(d) = sum_i  1 / (k + r_i)

with `k` defaulting to 60 (the value from the paper; widely used).

A document that appears in only one list still gets a score — RRF
is intentionally lenient about partial overlap, which is exactly
what we want when dense and BM25 disagree on what's relevant.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Sequence

from app.repositories.embedding_search import CandidateRow
from app.services.document.analyser import rank_top_rows
from app.services.document.retrieval.bm25 import (
    BM25Index,
    build_bm25_index,
    bm25_top_rows,
)


# Defaults chosen for the Doc2Dial eval corpus — see ADR-018 (to be
# filed when this lands). These are the same numbers we expect to
# validate on the eval harness before changing `Settings` defaults.
DEFAULT_DENSE_K: int = 15
DEFAULT_BM25_K: int = 15
DEFAULT_RRF_K: int = 60


def reciprocal_rank_fusion(
    ranked_lists: Sequence[Sequence[CandidateRow]],
    *,
    k: int = DEFAULT_RRF_K,
) -> list[tuple[CandidateRow, float]]:
    """Fuse N ranked orderings into one fused ordering.

    Args:
        ranked_lists: One list of `CandidateRow` per retrieval signal,
            in descending relevance order. Empty lists are allowed
            (they contribute zero scores to every chunk).
        k: RRF constant. 60 by default (Cormack 2009). Larger k
            flattens rank differences; smaller k sharpens them.

    Returns:
        `list[(CandidateRow, fused_score)]` in descending fused score
        order. `fused_score` is the sum of `1/(k + rank)` across all
        lists in which the chunk appeared. Ties broken by
        `(doc_id, page_number, chunk_index)` for determinism — same
        secondary order as `rank_top_rows`.

    Dedup:
        By `chunk_id`. Each unique chunk appears at most once in the
        output, with its scores accumulated across lists.
    """
    if k <= 0:
        raise ValueError(f"RRF constant k must be > 0, got {k}")

    scores: dict[str, float] = defaultdict(float)
    by_id: dict[str, CandidateRow] = {}

    for ranked in ranked_lists:
        for rank, row in enumerate(ranked, start=1):
            cid = str(row.chunk_id)
            scores[cid] += 1.0 / (k + rank)
            by_id[cid] = row

    if not scores:
        return []

    # Sort by fused score desc, then deterministic secondary key.
    # We tie-break on the row from `by_id[cid]` (all identical per id)
    # using the same tuple `rank_top_rows` uses, so dense-vs-BM25-vs-RRF
    # tie order is consistent end-to-end.
    fused_sorted = sorted(
        scores.items(),
        key=lambda kv: (
            -kv[1],
            str(by_id[kv[0]].document_id),
            by_id[kv[0]].page_number,
            by_id[kv[0]].chunk_index,
        ),
    )
    return [(by_id[cid], s) for cid, s in fused_sorted]


def hybrid_retrieve(
    candidates: Sequence[CandidateRow],
    query_vector: Sequence[float],
    query_text: str,
    *,
    dense_top_k: int = DEFAULT_DENSE_K,
    bm25_top_k: int = DEFAULT_BM25_K,
    rrf_k: int = DEFAULT_RRF_K,
    top_k: int = 5,
    min_cosine: float = 0.0,
    bm25_index: BM25Index | None = None,
) -> list[tuple[CandidateRow, float]]:
    """Dense top-K + BM25 top-K → RRF → final top-K.

    Args:
        candidates: Already-fetched candidate set (output of
            `fetch_candidate_rows`). Same input shape as
            `rank_top_rows`.
        query_vector: Embedding of `query_text`, produced by the same
            `EmbeddingModel` that produced the chunk vectors. Caller
            is responsible for dim equality (same invariant as
            `rank_top_rows`).
        query_text: Original query string. Needed by BM25 (lexical);
            not used by the dense leg.
        dense_top_k: How many candidates to take from the dense
            ranking before fusion. We deliberately widen beyond
            `top_k` so RRF has signal to work with — `top_k=5` would
            leave BM25 nothing to add. 15 is the validated default.
        bm25_top_k: How many candidates to take from BM25 before
            fusion. Mirrors `dense_top_k`.
        rrf_k: RRF constant. 60 by default.
        top_k: Final number of chunks to return (matches prod
            `Settings.analysis_top_k`).
        min_cosine: Forwarded to `rank_top_rows`. Same semantics.
        bm25_index: Optional pre-built BM25 index. If provided,
            skips the per-call rebuild. Useful when the caller
            (e.g. the eval harness) wants to amortise build cost
            across many queries. **Caveat:** the index is over the
            candidate set, not the — for index reuse to be safe, the
            same `candidates` must be passed every call.

    Returns:
        `list[(CandidateRow, score)]` in descending fused score
        order. `score` is the RRF fused score (not normalised); use
        rank order, not the absolute score value.

    Empty candidates → empty result. Empty query_text → BM25 leg
    returns the deterministic fallback (`BM25Index._all_zero_top_k`);
    RRF still works because the dense leg carries the signal.
    """
    if not candidates:
        return []
    if top_k <= 0:
        return []

    # 1. Dense top-K via the existing rank_top_rows — same code path
    #    prod uses today. Same tie-breakers, same min_cosine semantics.
    dense_ranked = rank_top_rows(
        list(candidates),
        query_vector,
        top_k=dense_top_k,
        min_cosine=min_cosine,
    )
    dense_rows = [row for row, _score in dense_ranked]

    # 2. BM25 top-K. Build lazily when the caller didn't pass one.
    #    Per-call build is ~100ms on 6k chunks (acceptable); per-run
    #    reuse is faster and what the eval harness does.
    if bm25_index is None:
        bm25_index = build_bm25_index(candidates)
    bm25_ranked = bm25_top_rows(
        bm25_index,
        query_text,
        top_k=bm25_top_k,
    )
    bm25_rows = [row for row, _score in bm25_ranked]

    # 3. Fuse. RRF dedupes by chunk_id, so a chunk that appears in
    #    both legs is counted twice (once per leg) — this is the
    #    whole reason RRF works.
    fused = reciprocal_rank_fusion(
        [dense_rows, bm25_rows],
        k=rrf_k,
    )

    return fused[:top_k]


__all__ = [
    "DEFAULT_BM25_K",
    "DEFAULT_DENSE_K",
    "DEFAULT_RRF_K",
    "hybrid_retrieve",
    "reciprocal_rank_fusion",
]