"""BM25 (Okapi) ranking over a `CandidateRow` set.

In-memory, pure Python. Sufficient for the corpus sizes we see
(~10k chunks per user). No external deps, no DB.

Why in-memory and not SQLite FTS5 / Postgres tsvector:
  * The candidate set is already in memory (it comes from
    `fetch_candidate_rows`, which materialises `text` for every
    chunk into Python objects).
  * Building the BM25 index is O(N) over chunk text — on 6k chunks
    it's ~100 ms. Per-query scoring is O(|query|) — sub-millisecond.
  * No migration / no extra DB connection per query / no port split
    between dev (SQLite) and prod (Postgres).

Algorithm: Okapi BM25 with standard `k1=1.5, b=0.75` defaults.
Score per (query_term, chunk) is:

    IDF(term) * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avgdl))

where `dl` is the chunk's token count and `avgdl` is the mean across
the indexed set. `IDF` uses the Robertson–Sparck Jones variant with
`+0.5` smoothing on both sides so unseen-in-corpus terms don't
explode the score to log(N/0.5) (we never score those anyway because
the query is tokenised from the same vocabulary).

Returns `list[(CandidateRow, score)]` in descending score order. A
query with zero BM25 overlap still returns `top_k` rows — every row
gets a non-negative BM25 score (≥ 0) because IDF is bounded, so
ties / zeros collapse to the deterministic secondary order
(document_id, page_number, chunk_index).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from app.repositories.embedding_search import CandidateRow
from app.services.document.retrieval._tokenize import tokenize_cached


@dataclass(frozen=True)
class BM25Index:
    """Immutable index over one candidate set.

    Built once via `build_bm25_index`; queried via `bm25_top_rows`
    or directly via `query` (returns scores for every chunk).

    Fields are read-only dataclasses. `idf` and `doc_lens` use plain
    dicts so they pickle cleanly (no weakref surprises) and so
    pytest fixtures can deep-copy the index cheaply.
    """

    candidates: tuple[CandidateRow, ...]
    # term -> idf weight (float). Terms not in this map have IDF 0
    # and contribute zero to the BM25 sum.
    idf: dict[str, float]
    # chunk index in `candidates` -> token count (int)
    doc_lens: tuple[int, ...]
    avg_dl: float
    k1: float
    b: float

    def query(self, query_text: str, top_k: int) -> list[tuple[CandidateRow, float]]:
        """Return up to `top_k` (row, score) pairs in descending score.

        Empty query text → deterministic order (stable, by
        doc_id/page/chunk_index) with all-zero BM25 scores. Caller
        can detect "BM25 had no signal" by checking
        `max(s for _, s in result) == 0.0` if needed.
        """
        if top_k <= 0 or not self.candidates:
            return []

        terms = tokenize_cached(query_text)
        if not terms:
            return self._all_zero_top_k(top_k)

        # Pre-resolve idf for the query terms. Terms absent from the
        # corpus contribute 0 (defensive — tokenise_cached filters
        # stopwords, so this is rare).
        term_idfs: list[float] = []
        for t in terms:
            w = self.idf.get(t)
            if w is not None and w > 0.0:
                term_idfs.append(w)
        if not term_idfs:
            return self._all_zero_top_k(top_k)

        # Score every chunk. For 6k chunks × ~5 query terms this is
        # ~30k token lookups — well under a millisecond.
        scores: list[float] = [0.0] * len(self.candidates)
        for i, _row in enumerate(self.candidates):
            # Use the precomputed tokens from build_bm25_index. We
            # didn't store them on the index (memory); recompute from
            # cached text. tokenize_cached returns a tuple, so we
            # can iterate multiple times safely.
            row_tokens = tokenize_cached(self.candidates[i].text)
            if not row_tokens:
                continue
            dl = self.doc_lens[i]
            denom_const = 1.0 - self.b + self.b * (dl / self.avg_dl)
            # Build tf map for this chunk (cheap; chunks are short).
            tf_map: dict[str, int] = {}
            for t in row_tokens:
                tf_map[t] = tf_map.get(t, 0) + 1
            # Sum BM25 over query terms present in this chunk.
            s = 0.0
            for j, term in enumerate(terms):
                tf = tf_map.get(term)
                if tf is None or tf == 0:
                    continue
                idf = term_idfs[j] if j < len(term_idfs) else self.idf.get(term, 0.0)
                if idf <= 0.0:
                    continue
                num = tf * (self.k1 + 1.0)
                den = tf + self.k1 * denom_const
                s += idf * (num / den)
            scores[i] = s

        # Sort by score desc; stable secondary key (doc_id, page, chunk)
        # mirrors `rank_top_rows` so dense+BM25 rankings have
        # consistent tie behaviour.
        order = sorted(
            range(len(self.candidates)),
            key=lambda i: (
                -scores[i],
                str(self.candidates[i].document_id),
                self.candidates[i].page_number,
                self.candidates[i].chunk_index,
            ),
        )
        return [
            (self.candidates[i], scores[i]) for i in order[:top_k]
        ]

    def _all_zero_top_k(self, top_k: int) -> list[tuple[CandidateRow, float]]:
        """Deterministic fallback when the query has no BM25 signal.

        RRF relies on every list being non-empty for the dense/BM25
        scores to be comparable. By returning a deterministic top-K
        with score=0, RRF will still favour dense-list ranks for
        these chunks (a chunk in the BM25-zero list at rank N gets
        1/(k+N); same chunk in dense-top-K gets 1/(k+rank_dense);
        the dense rank dominates whenever dense_rank < N). That is
        the desired property.
        """
        order = sorted(
            range(len(self.candidates)),
            key=lambda i: (
                str(self.candidates[i].document_id),
                self.candidates[i].page_number,
                self.candidates[i].chunk_index,
            ),
        )
        return [
            (self.candidates[i], 0.0) for i in order[:top_k]
        ]


def build_bm25_index(
    candidates: Sequence[CandidateRow],
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> BM25Index:
    """Build an immutable BM25 index over `candidates`.

    Steps:
      1. Tokenise every chunk's text (cached).
      2. Compute per-chunk token count + average doc length.
      3. Compute document frequency per term.
      4. Convert to IDF via the Robertson–Sparck Jones formula:

         IDF(t) = log( (N - df(t) + 0.5) / (df(t) + 0.5) + 1 )

         The `+1` keeps IDF non-negative even when a term is in every
         chunk (df=N). With Okapi BM25, negative IDF terms are
         typically dropped — we don't drop them, we just zero their
         contribution in `query`. Simpler and safer.

    Returns a `BM25Index` whose `query()` is the only hot path.
    """
    n = len(candidates)
    if n == 0:
        return BM25Index(
            candidates=tuple(),
            idf={},
            doc_lens=tuple(),
            avg_dl=0.0,
            k1=k1,
            b=b,
        )

    doc_lens: list[int] = []
    df: dict[str, int] = {}
    total_len = 0

    for row in candidates:
        tokens = tokenize_cached(row.text)
        doc_lens.append(len(tokens))
        total_len += len(tokens)
        # Count each term once per chunk (standard BM25 df).
        seen: set[str] = set()
        for t in tokens:
            if t in seen:
                continue
            seen.add(t)
            df[t] = df.get(t, 0) + 1

    avg_dl = total_len / n if n else 0.0

    idf: dict[str, float] = {}
    for term, freq in df.items():
        # Robertson–Sparck Jones with +1 inside the log.
        idf[term] = math.log(((n - freq + 0.5) / (freq + 0.5)) + 1.0)

    return BM25Index(
        candidates=tuple(candidates),
        idf=idf,
        doc_lens=tuple(doc_lens),
        avg_dl=avg_dl,
        k1=k1,
        b=b,
    )


def bm25_top_rows(
    index: BM25Index,
    query_text: str,
    *,
    top_k: int,
) -> list[tuple[CandidateRow, float]]:
    """Thin wrapper for callers who don't want to know about the index.

    Same signature shape as `rank_top_rows` minus the query vector
    (BM25 is text-only).
    """
    return index.query(query_text, top_k)


__all__ = ["BM25Index", "build_bm25_index", "bm25_top_rows"]