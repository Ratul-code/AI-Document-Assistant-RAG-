"""Recursive token-aware text splitter.

Splits long text into chunks sized for embedding models. Levels:

    1. Paragraphs   (split on blank lines)
    2. Sentences    (., !, ? followed by whitespace)
    3. Words        (whitespace)
    4. Characters   (last resort — chunked hard)

At each level we try to merge the pieces back together until we hit
the target token count. Anything that would push us past the max is
recursively split at the next level. Chunks emitted close to the
target carry the next chunk's first `OVERLAP_TOKENS` tokens so each
chunk has a little context for the LLM.

Counts are measured in tokens via tiktoken (cl100k_base) so the chunk
sizes track real embedding-model budgets, not raw characters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

import tiktoken

# Defaults — exported because the chunker orchestration reads them.
TARGET_TOKENS = 500
MAX_TOKENS = 800
OVERLAP_TOKENS = 100

# Separator regexes. Order matters: try the biggest first.
_PARAGRAPH_RE = re.compile(r"\n\s*\n")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_WORD_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class TextChunk:
    """A single chunk produced by the recursive splitter.

    `text`         - the chunk's own content (no overlap)
    `token_count`  - tokens in `text` only
    `overlap_text` - the next chunk's first OVERLAP_TOKENS tokens, or ""
    """

    text: str
    token_count: int
    overlap_text: str = ""


@lru_cache(maxsize=1)
def _encoder() -> tiktoken.Encoding:
    """Cache the tiktoken encoder so we don't reload it on every chunk."""
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """Token count for `text` using the cached encoder."""
    return len(_encoder().encode(text))


def _encode_ids(text: str) -> list[int]:
    return _encoder().encode(text)


def _decode_ids(ids: list[int]) -> str:
    return _encoder().decode(ids)


def split_text(text: str) -> list[TextChunk]:
    """Split `text` into token-bounded chunks.

    Returns the final list of chunks. Order preserved. Empty input
    yields an empty list. Tokens are measured with tiktoken so chunk
    sizes match the embedding model.
    """
    cleaned = text.strip()
    if not cleaned:
        return []

    pieces = [p.strip() for p in _PARAGRAPH_RE.split(cleaned) if p.strip()]
    if not pieces:
        return []

    merged = _merge_recursive(pieces, level=0)
    return _attach_overlap(merged)


def _merge_recursive(pieces: list[str], *, level: int) -> list[str]:
    """Merge `pieces` back together, splitting any that exceed MAX_TOKENS.

    `level` selects the split regex:
        0 -> paragraph (re.split on blank lines)
        1 -> sentence  (re.split on sentence-end whitespace)
        2 -> word      (re.split on whitespace)
        3 -> char      (hard cut, last resort)
    """
    if level > 3:
        # Hard cap fallback: keep the oversize piece as a single chunk.
        return pieces

    chunks: list[str] = []
    buffer: list[str] = []
    buffer_tokens = 0

    for piece in pieces:
        piece_tokens = count_tokens(piece)

        # Single piece already too big — split at the next level.
        if piece_tokens > MAX_TOKENS:
            if buffer:
                chunks.append(" ".join(buffer).strip())
                buffer, buffer_tokens = [], 0
            chunks.extend(_split_oversize(piece, level=level))
            continue

        # Adding this piece would push us past the target — flush.
        if buffer and buffer_tokens + piece_tokens > TARGET_TOKENS:
            chunks.append(" ".join(buffer).strip())
            buffer, buffer_tokens = [piece], piece_tokens
            continue

        buffer.append(piece)
        buffer_tokens += piece_tokens

    if buffer:
        chunks.append(" ".join(buffer).strip())

    return chunks


def _split_oversize(piece: str, *, level: int) -> list[str]:
    """Split a piece that's too big at the given level.

    If splitting at this level produces nothing useful (e.g. a paragraph
    with no sentence breaks), the next level is tried.
    """
    if level == 3:
        # Hard char split — pack as many whole tokens as fit.
        return _hard_char_split(piece)

    pattern = (_PARAGRAPH_RE, _SENTENCE_RE, _WORD_RE)[level]
    parts = [p.strip() for p in pattern.split(piece) if p.strip()]
    if not parts or len(parts) == 1:
        return _merge_recursive([piece], level=level + 1)
    return _merge_recursive(parts, level=level + 1)


def _hard_char_split(piece: str) -> list[str]:
    """Split a piece that's too big at the character level."""
    ids = _encode_ids(piece)
    chunks: list[str] = []
    step = MAX_TOKENS
    for start in range(0, len(ids), step):
        chunk_ids = ids[start : start + step]
        if chunk_ids:
            chunks.append(_decode_ids(chunk_ids))
    return chunks


def _attach_overlap(chunks: list[str]) -> list[TextChunk]:
    """Annotate each chunk with the next chunk's first OVERLAP tokens.

    Encodes each chunk exactly once and slices the token-id arrays to
    produce overlap text. Avoids the O(N²) re-encode that would happen
    if we re-encoded the whole next chunk just to grab its first 100
    tokens.
    """
    if not chunks:
        return []

    encoded: list[list[int]] = [_encode_ids(c) for c in chunks]
    token_counts = [len(ids) for ids in encoded]

    result: list[TextChunk] = []
    for idx, chunk in enumerate(chunks):
        overlap_block = ""
        if idx + 1 < len(chunks):
            next_prefix = encoded[idx + 1][:OVERLAP_TOKENS]
            if next_prefix:
                overlap_block = _decode_ids(next_prefix)
        result.append(
            TextChunk(
                text=chunk,
                token_count=token_counts[idx],
                overlap_text=overlap_block,
            )
        )
    return result


__all__ = [
    "MAX_TOKENS",
    "OVERLAP_TOKENS",
    "TARGET_TOKENS",
    "TextChunk",
    "build_chunks_for_page",
    "count_tokens",
    "split_text",
]


# Re-export the orchestration helper so callers only need to import the
# chunker package, not individual modules.
from .build_chunks import build_chunks_for_page  # noqa: E402
