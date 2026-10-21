r"""Passage -> chunk fuzzy-overlap mapping.

The Doc2Dial dataset gives us gold-relevance at the *passage*
level: each user turn references evidence spans
(`{"sp_id": ..., "label": ...}`) inside the source document. Our
chunker, however, splits the source documents at our 500/800-token
tiktoken boundaries, which do not coincide with the dataset's span
boundaries. So we cannot use a 1:1 passage→chunk map.

Instead we use a fuzzy heuristic:

    overlap_ratio(P, C) = longest_common_substring_length(P, C) / len(P)

A chunk `C` is marked relevant to a question iff
`overlap_ratio(P, C) >= threshold` for at least one gold passage `P`
of that question. Default threshold is 0.5.

Why longest common substring (not set-similarity, not BLEU, not
cosine):
* LCS handles chunk-boundary passages naturally: a passage that
  spans two chunks gets its halves matched independently and both
  chunks are marked relevant.
* LCS is robust to whitespace and punctuation drift between
  Doc2Dial's span text and our tiktoken chunker.
* LCS is a pure stdlib algorithm. No numpy, no tokenisation, no
  embedder — easy to reason about and audit.

Cost: O(|P| · |C|) per pair. Passages are ≤ ~2 KB, chunks are ≤
~1.5 KB (the 800-token cap). With ~6 docs × ~13 chunks and 40
questions, the mapping pass is sub-second.

Both sides are whitespace-normalised (`re.sub(r"\s+", " ", ...)`)
before scoring so a passage joined from sentences with one space
matches a chunk joined from paragraphs with another.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from uuid import UUID

from eval.schema import PassageMapping, PassageMappingEntry

_WHITESPACE_RE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    """Collapse all whitespace runs to a single space and strip."""
    return _WHITESPACE_RE.sub(" ", text or "").strip()


def lcs_ratio(passage: str, chunk: str, *, normalised: bool = True) -> float:
    """Longest common substring length divided by `len(passage)`.

    Returns 0.0 if either string is empty (or becomes empty after
    whitespace normalisation).

    If `normalised=True` (default), both sides are passed through
    `_normalise()` first.
    """
    a = _normalise(passage) if normalised else (passage or "")
    b = _normalise(chunk) if normalised else (chunk or "")
    if not a or not b:
        return 0.0
    n, m = len(a), len(b)
    if m == 0:
        return 0.0

    # Two-row DP: row N-1 only is needed to compute row N.
    prev = [0] * (m + 1)
    best = 0
    for i in range(1, n + 1):
        cur = [0] * (m + 1)
        ai = a[i - 1]
        for j in range(1, m + 1):
            if ai == b[j - 1]:
                value = prev[j - 1] + 1
                cur[j] = value
                if value > best:
                    best = value
        prev = cur
    return best / n


def build_passage_mapping(
    *,
    question_id: str,
    document_id: UUID,
    passages: Iterable[tuple[str, str]],  # (passage_id, passage_text)
    chunks: list[tuple[int, str]],  # (chunk_index, chunk_text)
    threshold: float = 0.5,
    dataset_version: str = "v1",
) -> tuple[list[tuple[UUID, int]], PassageMapping]:
    """For one question, mark each chunk relevant iff overlap_ratio ≥ threshold.

    The "key" for a relevant chunk is `(document_id, chunk_index)`,
    not a UUID — chunker-assigned UUIDs are non-deterministic across
    re-seeds (`uuid4()`), so the dataset must reference chunks by
    a stable key that survives the hermetic rebuild.

    Args:
        question_id: included in the audit mapping for traceability.
        document_id: the document all chunks in this call belong to
            (Doc2Dial maps one passage list to one document).
        passages: `(passage_id, passage_text)` tuples — typically the
            gold evidence spans for this question.
        chunks: `(chunk_index, chunk_text)` tuples — one per chunk of
            `document_id`, in chunk_index order.
        threshold: LCS overlap fraction above which a passage→chunk
            assignment is marked relevant.

    Returns:
        relevant_chunk_keys: deduplicated list of `(document_id,
            chunk_index)` tuples marked relevant. Order is the order
            they appear in `chunks`.
        mapping: a `PassageMapping` populated with one
            `PassageMappingEntry` per (passage, chunk) match above
            the threshold. Audit-only; the runner ignores it.
    """
    relevant: list[tuple[UUID, int]] = []
    seen_relevant: set[tuple[UUID, int]] = set()
    entries: list[PassageMappingEntry] = []

    for passage_id, passage_text in passages:
        passage_norm = _normalise(passage_text)
        if not passage_norm:
            # Doc2Dial occasionally has empty passage text — skip
            # silently. The build_dataset step will count + report.
            continue
        for chunk_index, chunk_text in chunks:
            ratio = lcs_ratio(passage_norm, chunk_text, normalised=False)
            if ratio >= threshold:
                key = (document_id, chunk_index)
                entries.append(
                    PassageMappingEntry(
                        passage_id=passage_id,
                        passage_text=passage_text,
                        document_id=document_id,
                        chunk_index=chunk_index,
                        chunk_text=chunk_text,
                        overlap_ratio=ratio,
                    )
                )
                if key not in seen_relevant:
                    seen_relevant.add(key)
                    relevant.append(key)

    mapping = PassageMapping(
        dataset_version=dataset_version,
        heuristic="fuzzy_overlap_0.5",
        threshold=threshold,
        entries=entries,
    )
    return relevant, mapping


__all__ = ["build_passage_mapping", "lcs_ratio"]
