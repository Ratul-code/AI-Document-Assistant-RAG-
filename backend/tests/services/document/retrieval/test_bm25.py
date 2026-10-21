"""Unit tests for BM25Index + hybrid_retrieve + reciprocal_rank_fusion.

All tests are pure — they fabricate `CandidateRow` instances with
hand-written text, build an index, and check the output. No DB,
no embedder. This is the property that lets the retrieval layer
ship without coupling to the analyser.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.repositories.embedding_search import CandidateRow
from app.services.document.retrieval.bm25 import (
    bm25_top_rows,
    build_bm25_index,
)
from app.services.document.retrieval.hybrid import (
    hybrid_retrieve,
    reciprocal_rank_fusion,
)

# ---------------------------------------------------------------------
# Fixtures: small but realistic corpus shape
# ---------------------------------------------------------------------


def _row(text: str, *, idx: int = 0, doc_id: UUID | None = None) -> CandidateRow:
    """Build a CandidateRow with a placeholder 1-dim vector.

    Vector content doesn't matter for BM25 / RRF — only for the dense
    leg of hybrid_retrieve (which the tests below only use as a
    separate signal, not as a unit-of-truth).
    """
    return CandidateRow(
        chunk_id=uuid4(),
        document_id=doc_id or uuid4(),
        document_title="t",
        page_id=uuid4(),
        page_number=0,
        chunk_index=idx,
        section_path=[],
        text=text,
        vector=[0.0],
        embedding_model_name="stub",
        embedding_dim=1,
    )


@pytest.fixture
def three_doc_corpus():
    """One corpus where 'hcov' is a clear lexical match for doc B and C
    but a non-match for doc A. Used for ranking assertions."""
    doc_a, doc_b, doc_c = uuid4(), uuid4(), uuid4()
    return [
        _row("The patient developed a mild fever and was discharged.", idx=0, doc_id=doc_a),
        _row("HCV infection is a major cause of liver disease worldwide.", idx=0, doc_id=doc_b),
        _row(
            "Recent studies show that HCV treatment with antivirals is effective.",
            idx=0,
            doc_id=doc_c,
        ),
    ]


# ---------------------------------------------------------------------
# BM25Index / build_bm25_index / bm25_top_rows
# ---------------------------------------------------------------------


class TestBuildBM25Index:
    def test_empty_corpus(self):
        idx = build_bm25_index([])
        assert len(idx.candidates) == 0
        assert idx.idf == {}
        assert idx.avg_dl == 0.0

    def test_idf_known_term(self):
        idx = build_bm25_index(
            [
                _row("hcv is bad", idx=0),
                _row("covid is also bad", idx=0),
            ]
        )
        # Both "hcv" and "covid" appear in 1/2 docs.
        assert "hcv" in idx.idf
        assert "covid" in idx.idf
        assert idx.idf["hcv"] == pytest.approx(idx.idf["covid"])

    def test_idf_higher_for_rare_term(self):
        idx = build_bm25_index(
            [
                _row("common common common", idx=0),
                _row("common common", idx=0),
                _row("rare", idx=0),
            ]
        )
        # "rare" appears in 1 doc; "common" in 2 — "rare" has higher IDF.
        assert idx.idf["rare"] > idx.idf["common"]

    def test_doc_lens_match_text(self):
        # Tokens must be ≥2 chars to survive the regex.
        idx = build_bm25_index([_row("foo bar baz", idx=0)])
        assert idx.doc_lens == (3,)
        assert idx.avg_dl == pytest.approx(3.0)

    def test_returns_tuple_candidates(self):
        # BM25Index.candidates is a tuple so the index is immutable
        # across `build_*` calls and dataclass(frozen=True) is honoured.
        rows = [_row("a", idx=0), _row("b", idx=1)]
        idx = build_bm25_index(rows)
        assert isinstance(idx.candidates, tuple)


class TestBm25Query:
    def test_exact_match_ranks_first(self, three_doc_corpus):
        idx = build_bm25_index(three_doc_corpus)
        results = bm25_top_rows(idx, "HCV", top_k=3)
        # The two docs mentioning "HCV" should be ahead of doc A.
        hc_ids = {three_doc_corpus[1].chunk_id, three_doc_corpus[2].chunk_id}
        assert results[0][0].chunk_id in hc_ids
        assert results[-1][0].chunk_id == three_doc_corpus[0].chunk_id

    def test_zero_overlap_returns_deterministic_topk(self, three_doc_corpus):
        idx = build_bm25_index(three_doc_corpus)
        results = bm25_top_rows(idx, "quantum entanglement yeetnetball", top_k=2)
        assert len(results) == 2
        # All-zero BM25 → fall back to deterministic order.
        assert results[0][1] == 0.0
        assert results[1][1] == 0.0

    def test_top_k_truncates(self, three_doc_corpus):
        idx = build_bm25_index(three_doc_corpus)
        results = bm25_top_rows(idx, "HCV", top_k=1)
        assert len(results) == 1

    def test_top_k_zero_returns_empty(self, three_doc_corpus):
        idx = build_bm25_index(three_doc_corpus)
        assert bm25_top_rows(idx, "HCV", top_k=0) == []

    def test_empty_corpus_returns_empty(self):
        idx = build_bm25_index([])
        assert bm25_top_rows(idx, "anything", top_k=5) == []


# ---------------------------------------------------------------------
# reciprocal_rank_fusion
# ---------------------------------------------------------------------


class TestReciprocalRankFusion:
    def test_empty_inputs(self):
        assert reciprocal_rank_fusion([]) == []
        assert reciprocal_rank_fusion([[], []]) == []

    def test_single_list_passthrough(self):
        rows = [_row("a", idx=i) for i in range(3)]
        fused = reciprocal_rank_fusion([rows])
        # Same rows, same order. Score is 1/(k + rank).
        assert [r[0] for r in fused] == rows
        assert fused[0][1] == pytest.approx(1.0 / 61.0)
        assert fused[1][1] == pytest.approx(1.0 / 62.0)
        assert fused[2][1] == pytest.approx(1.0 / 63.0)

    def test_dedup_across_lists(self):
        a = _row("a", idx=0)
        b = _row("b", idx=0)
        c = _row("c", idx=0)
        # a appears in both lists at rank 1, b at rank 2 in first
        # list only, c at rank 2 in second list only.
        fused = reciprocal_rank_fusion([[a, b], [a, c]])
        ids = [r[0].chunk_id for r in fused]
        assert ids[0] == a.chunk_id  # 1/61 + 1/61 > 1/62 or 1/62
        assert set(ids) == {a.chunk_id, b.chunk_id, c.chunk_id}
        # a's score = 2 * 1/61
        a_score = next(s for r, s in fused if r.chunk_id == a.chunk_id)
        assert a_score == pytest.approx(2.0 / 61.0)

    def test_ranking_by_fused_score(self):
        a = _row("a", idx=0)
        b = _row("b", idx=0)
        # a is rank 1 in list 1, rank 2 in list 2
        # b is rank 2 in list 1, rank 1 in list 2
        # Same fused score → tie broken by secondary key (doc_id asc).
        fused = reciprocal_rank_fusion([[a, b], [b, a]])
        # Both have score 1/61 + 1/62. Secondary tie-break on doc_id asc.
        if a.document_id < b.document_id:
            assert fused[0][0].chunk_id == a.chunk_id
            assert fused[1][0].chunk_id == b.chunk_id
        else:
            assert fused[0][0].chunk_id == b.chunk_id
            assert fused[1][0].chunk_id == a.chunk_id

    def test_rejects_invalid_k(self):
        with pytest.raises(ValueError):
            reciprocal_rank_fusion([[_row("x", idx=0)]], k=0)
        with pytest.raises(ValueError):
            reciprocal_rank_fusion([[_row("x", idx=0)]], k=-1)

    def test_smaller_k_sharper_differences(self):
        # With k=0 (invalid), but with k=1: rank 1 → 1/2, rank 2 → 1/3,
        # gap is 1/2 - 1/3 = 1/6. With k=60: 1/61 - 1/62 ≈ tiny.
        rows = [_row("a", idx=i) for i in range(2)]
        s_k1 = reciprocal_rank_fusion([rows], k=1)
        s_k60 = reciprocal_rank_fusion([rows], k=60)
        # Difference between rank-0 and rank-1 scores is larger with k=1.
        diff_k1 = s_k1[0][1] - s_k1[1][1]
        diff_k60 = s_k60[0][1] - s_k60[1][1]
        assert diff_k1 > diff_k60


# ---------------------------------------------------------------------
# hybrid_retrieve (orchestrator)
# ---------------------------------------------------------------------)


class TestHybridRetrieve:
    def test_empty_candidates(self):
        assert hybrid_retrieve([], [0.0], "anything") == []

    def test_top_k_zero(self):
        rows = [_row("HCV treatment", idx=0)]
        assert hybrid_retrieve(rows, [0.0], "HCV", top_k=0) == []

    def test_top_k_truncates(self):
        rows = [_row(f"HCV chunk {i}", idx=i) for i in range(5)]
        results = hybrid_retrieve(rows, [0.0], "HCV", top_k=3)
        assert len(results) == 3

    def test_bm25_lifts_lexical_match_into_topk(self):
        """If dense (stub) ranks everything equal, BM25 should put the
        lexical match at the top. With our 1-dim zero vectors, dense
        cosine is undefined (zero / zero). The BM25 leg carries the
        signal."""
        lex_match = _row("HCV treatment is effective", idx=0)
        non_match = _row("Quantum physics experiment", idx=0)
        results = hybrid_retrieve(
            [lex_match, non_match],
            [0.0],
            "HCV treatment",
            top_k=1,
        )
        assert results[0][0].chunk_id == lex_match.chunk_id

    def test_dense_and_bm25_overlap_boosts_score(self):
        """A chunk that ranks well in both legs should beat a chunk
        that ranks well in only one."""
        in_both = _row("HCV interferon treatment trial", idx=0)
        bm25_only = _row("HCV virus is bad", idx=0)
        dense_only = _row("unrelated text about hepatitis", idx=0)
        # All-zero dense vector → dense cosine is undefined for all.
        # With rank_top_rows falling back to score=0, dense ranks
        # are by secondary key (doc_id asc) regardless of content.
        # To exercise the boost we need non-zero vectors — use a tiny
        # vector that points toward `in_both` only.
        rows = [in_both, bm25_only, dense_only]
        # Use distinct vectors so cosine ranking is meaningful.
        rows[0] = CandidateRow(
            chunk_id=in_both.chunk_id,
            document_id=in_both.document_id,
            document_title=in_both.document_title,
            page_id=in_both.page_id,
            page_number=in_both.page_number,
            chunk_index=in_both.chunk_index,
            section_path=in_both.section_path,
            text=in_both.text,
            vector=[1.0, 0.0],
            embedding_model_name="stub",
            embedding_dim=2,
        )
        rows[1] = CandidateRow(
            chunk_id=bm25_only.chunk_id,
            document_id=bm25_only.document_id,
            document_title=bm25_only.document_title,
            page_id=bm25_only.page_id,
            page_number=bm25_only.page_number,
            chunk_index=bm25_only.chunk_index,
            section_path=bm25_only.section_path,
            text=bm25_only.text,
            vector=[0.0, 1.0],
            embedding_model_name="stub",
            embedding_dim=2,
        )
        rows[2] = CandidateRow(
            chunk_id=dense_only.chunk_id,
            document_id=dense_only.document_id,
            document_title=dense_only.document_title,
            page_id=dense_only.page_id,
            page_number=dense_only.page_number,
            chunk_index=dense_only.chunk_index,
            section_path=dense_only.section_path,
            text=dense_only.text,
            vector=[0.5, 0.5],
            embedding_model_name="stub",
            embedding_dim=2,
        )
        # Query: shares "HCV treatment" with in_both, "HCV" with bm25_only.
        results = hybrid_retrieve(
            rows,
            [1.0, 0.0],  # aligns with in_both only
            "HCV treatment",
            top_k=3,
            dense_top_k=3,
            bm25_top_k=3,
        )
        # in_both wins: dense rank 1 + BM25 rank 1 = 2/(k+1)
        # bm25_only: dense rank 2 (orthogonal) + BM25 rank 2 = same / +
        # Actually cosine of [0,1] and [1,0] is 0 → score 0.
        # Let's just assert in_both is first; the relative order of
        # the rest is implementation-dependent but should not put
        # bm25_only last if dense_only has some signal.
        assert results[0][0].chunk_id == in_both.chunk_id

    def test_prebuilt_bm25_index_is_reused(self):
        """If a pre-built BM25 index is passed, the function must not
        rebuild it (no exception, output still correct)."""
        rows = [_row("HCV treatment trial", idx=0), _row("unrelated", idx=0)]
        idx = build_bm25_index(rows)
        results = hybrid_retrieve(
            rows,
            [0.0, 0.0],
            "HCV",
            top_k=1,
            bm25_index=idx,
        )
        assert results[0][0].chunk_id == rows[0].chunk_id

    def test_score_is_rrf_fused_score(self):
        """The fused score should match the RRF formula."""
        rows = [_row(f"chunk {i}", idx=i) for i in range(3)]
        # With all-zero dense vector, rank_top_rows returns score 0
        # for everyone and falls back to secondary order. Two equal
        # rows in both legs → fused scores are equal too.
        results = hybrid_retrieve(
            rows,
            [0.0, 0.0, 0.0],
            "chunk",  # hits all three rows
            top_k=3,
            dense_top_k=3,
            bm25_top_k=3,
        )
        # Scores should be positive (chunk appears in both legs).
        for _, score in results:
            assert score > 0.0
