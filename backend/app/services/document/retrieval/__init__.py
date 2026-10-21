"""Hybrid retrieval: dense top-K + BM25 top-K → Reciprocal Rank Fusion.

Sibling to `app.services.document.analyser.rank_top_rows`. Same input
shape (`list[CandidateRow]` + query), same output shape
(`list[(CandidateRow, score)]`). Drop-in for the dense path.

Public surface:

* `BM25Index`            — precomputed BM25 index over a candidate set.
* `build_bm25_index`     — factory that tokenises + IDF-weights once.
* `bm25_top_rows`        — top-K BM25 ranking for a free-text query.
* `reciprocal_rank_fusion` — pure RRF over N input orderings.
* `hybrid_retrieve`      — orchestrator: dense top-K + BM25 top-K → RRF.

No I/O, no async. All functions are pure Python over already-fetched
`CandidateRow`s, which keeps the chunking / scoring / fusion pipeline
unit-testable without a DB session. The async wrapper at the call
site (`analyser.analyse_query`) puts the blocking parts behind
`asyncio.to_thread` — same pattern as `rank_top_rows`.
"""

from __future__ import annotations

from app.services.document.retrieval.bm25 import (
    BM25Index,
    bm25_top_rows,
    build_bm25_index,
)
from app.services.document.retrieval.hybrid import (
    hybrid_retrieve,
    reciprocal_rank_fusion,
)

__all__ = [
    "BM25Index",
    "build_bm25_index",
    "bm25_top_rows",
    "hybrid_retrieve",
    "reciprocal_rank_fusion",
]
