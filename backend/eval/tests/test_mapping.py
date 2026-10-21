"""Tests for the passage→chunk mapping heuristic.

Pin the LCS ratio function against hand-crafted strings, then check
that `build_passage_mapping` selects the right chunks at the
threshold boundary.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from eval.scripts.mapping import build_passage_mapping, lcs_ratio


C1 = UUID("00000000-0000-0000-0000-000000000001")
C2 = UUID("00000000-0000-0000-0000-000000000002")
C3 = UUID("00000000-0000-0000-0000-000000000003")


def test_lcs_ratio_full_overlap_is_one() -> None:
    p = "the quick brown fox"
    c = "the quick brown fox"
    assert lcs_ratio(p, c) == 1.0


def test_lcs_ratio_no_overlap_is_zero() -> None:
    assert lcs_ratio("abc", "xyz") == 0.0


def test_lcs_ratio_partial_is_proportional() -> None:
    # Passage is 19 chars; longest common substring is "the lazy "
    # (8 chars including trailing space) — wait, "the lazy " in p is
    # actually "the lazy " minus the space, so it is "the lazy" (8
    # chars). Either way the ratio should be in [0.3, 0.5].
    p = "the quick brown fox"
    c = "the lazy brown dog"
    ratio = lcs_ratio(p, c)
    # Longest common substring is "the lazy" (8 chars): 8/19 ≈ 0.421.
    assert 0.3 < ratio < 0.5


def test_lcs_ratio_uses_longest_substring() -> None:
    # Both sides have "abc" and "def" but the longest match is "abcdef".
    p = "abcdefghij"
    c = "xabcdefy"
    assert lcs_ratio(p, c) == 6 / 10


def test_lcs_ratio_empty_passage_is_zero() -> None:
    assert lcs_ratio("", "anything") == 0.0


def test_lcs_ratio_empty_chunk_is_zero() -> None:
    assert lcs_ratio("anything", "") == 0.0


def test_lcs_ratio_whitespace_normalisation() -> None:
    # Different whitespace conventions should not affect the ratio.
    p = "the quick brown fox"
    c = "  the   quick brown   fox  "
    assert lcs_ratio(p, c) == 1.0


def test_lcs_ratio_threshold_edge_inclusive() -> None:
    # Build a passage + chunk that hit exactly the 0.5 threshold.
    p = "the quick brown fox jumps"
    # Find a chunk whose LCS length is half of |p|.
    half = len(p) // 2  # 11 chars -> 5 for half of 22
    c = "x" * 100 + "the qu" + "y" * 90  # "the qu" = 6 chars
    # Overlap is "the qu" = 6 chars; ratio = 6/22 ≈ 0.27 (not at boundary)
    ratio = lcs_ratio(p, c)
    assert ratio < 0.5


def test_mapping_marks_relevant_chunks_above_threshold() -> None:
    # Two chunks; passage matches one exactly. Chunks are now keyed by
    # chunk_index (not chunk_id) — the document_id is passed separately.
    chunks = [
        (0, "the incubation period is five to six days"),
        (1, "remdesivir reduces recovery time"),
    ]
    passages = [("p0", "the incubation period is five to six days according to the WHO")]
    relevant, mapping = build_passage_mapping(
        question_id="q0",
        document_id=C1,  # any UUID; not used in matching
        passages=passages,
        chunks=chunks,
        threshold=0.5,
    )
    # (document_id, chunk_index) tuples are returned.
    assert (C1, 0) in relevant
    assert (C1, 1) not in relevant
    assert len(mapping.entries) == 1
    assert mapping.entries[0].chunk_index == 0


def test_mapping_drops_no_match() -> None:
    # No chunk matches at the 0.5 threshold → empty relevant set.
    chunks = [
        (0, "completely unrelated text about vaccines"),
        (1, "another unrelated sentence about masks"),
    ]
    passages = [("p0", "the incubation period is five to six days")]
    relevant, mapping = build_passage_mapping(
        question_id="q0",
        document_id=C1,
        passages=passages,
        chunks=chunks,
        threshold=0.5,
    )
    assert relevant == []
    assert mapping.entries == []


def test_mapping_dedups_duplicate_chunk_indices() -> None:
    # Two passages both match the same chunk_index → only one entry in
    # relevant list, two entries in the audit mapping.
    chunks = [(0, "the incubation period is five to six days")]
    passages = [
        ("p0", "the incubation period is five to six days"),
        ("p1", "the incubation period is five to six days according to WHO"),
    ]
    relevant, mapping = build_passage_mapping(
        question_id="q0",
        document_id=C1,
        passages=passages,
        chunks=chunks,
        threshold=0.5,
    )
    assert relevant == [(C1, 0)]
    assert len(mapping.entries) == 2


def test_mapping_threshold_edge_equal_is_inclusive() -> None:
    # Build a passage + chunk where the ratio equals the threshold
    # exactly. The threshold is inclusive (>=).
    # Passage length 10; we need LCS length 5.
    p = "abcdefghij"  # 10 chars
    c = "xxxxxabcdexxxxxxxxxxxxxxxxxx"  # LCS = "abcde" = 5 chars
    assert lcs_ratio(p, c) == 5 / 10
    chunks = [(0, c)]
    relevant, _ = build_passage_mapping(
        question_id="q0",
        document_id=C1,
        passages=[("p0", p)],
        chunks=chunks,
        threshold=0.5,
    )
    assert (C1, 0) in relevant


def test_mapping_skips_empty_passage() -> None:
    chunks = [(0, "anything")]
    passages = [("p0", ""), ("p1", "    ")]  # both empty after normalisation
    relevant, mapping = build_passage_mapping(
        question_id="q0",
        document_id=C1,
        passages=passages,
        chunks=chunks,
        threshold=0.5,
    )
    assert relevant == []
    assert mapping.entries == []