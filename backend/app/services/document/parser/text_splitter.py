from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_SOFT_TARGET_CHARS = 5000
_HARD_MAX_CHARS = 8000
_MIN_USABLE_CHARS = 20

_PARAGRAPH_BREAK = "\n\n"
_SENTENCE_END = re.compile(r"[.!?。！？]\s+(?=[A-Z\"'(\[])")
_HEADING_PREFIX = re.compile(r"^#{1,6}\s+")


@dataclass(frozen=True)
class _Chunk:
    text: str
    had_hard_break: bool = False


def split_into_pages(text: str, *, parser_name: str) -> list[_Chunk]:
    """Soft-5000-char splitter for markdown / plain text.

    Tries to keep pages close to 5000 chars. Breaks prefer paragraph
    boundaries, then line breaks, then sentence boundaries. The hard
    cap (8000) is the last resort — we may break inside a paragraph
    when a single paragraph exceeds the cap, and we mark that chunk
    `had_hard_break=True` so the caller can surface it in metadata.
    """
    if not text or not text.strip():
        return []

    paragraphs = _split_paragraphs(text)
    pages: list[_Chunk] = []
    buffer: list[str] = []
    buffer_len = 0
    had_hard_break = False

    for paragraph in paragraphs:
        if not paragraph:
            continue

        # A single paragraph can itself exceed the hard cap. Split it
        # greedily at sentence boundaries so we never break mid-sentence.
        if len(paragraph) > _HARD_MAX_CHARS:
            for piece in _split_oversized_paragraph(paragraph):
                pages.append(_Chunk(text=piece, had_hard_break=True))
            continue

        if buffer_len and buffer_len + len(paragraph) + len(_PARAGRAPH_BREAK) > _SOFT_TARGET_CHARS:
            chunk, _ = _flush_buffer(buffer, had_hard_break)
            if chunk is not None:
                pages.append(chunk)
            buffer, buffer_len, had_hard_break = [], 0, False

        buffer.append(paragraph)
        buffer_len += len(paragraph) + len(_PARAGRAPH_BREAK)

        if buffer_len >= _HARD_MAX_CHARS:
            chunk, _ = _flush_buffer(buffer, had_hard_break=True)
            if chunk is not None:
                pages.append(chunk)
            buffer, buffer_len, had_hard_break = [], 0, True

    chunk, _ = _flush_buffer(buffer, had_hard_break)
    if chunk is not None:
        pages.append(chunk)

    logger.debug(
        "%s splitter: produced %d pages from %d chars (target=%d, hard=%d)",
        parser_name, len(pages), len(text), _SOFT_TARGET_CHARS, _HARD_MAX_CHARS,
    )
    return pages


def _split_paragraphs(text: str) -> list[str]:
    """Normalize whitespace and split on paragraph boundaries."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return [
        part.strip()
        for part in re.split(r"\n{2,}", normalized)
        if part and part.strip()
    ]


def _split_oversized_paragraph(paragraph: str) -> list[str]:
    """Split a paragraph that exceeds the hard cap.

    Uses sentence boundaries first; falls back to fixed-width chunks if
    no sentence boundary is found in the window. Each chunk is marked
    `had_hard_break=True` by the caller.
    """
    chunks: list[str] = []
    remaining = paragraph
    while len(remaining) > _HARD_MAX_CHARS:
        window = remaining[: _HARD_MAX_CHARS]
        match = None
        for m in _SENTENCE_END.finditer(window):
            match = m
        if match is not None:
            cut = match.end()
        else:
            cut = _HARD_MAX_CHARS
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return [c for c in chunks if c]


def _flush_buffer(
    buffer: list[str], had_hard_break: bool
) -> tuple[_Chunk | None, bool]:
    """Join accumulated paragraphs and decide whether to keep the chunk."""
    if not buffer:
        return None, had_hard_break

    joined = _PARAGRAPH_BREAK.join(buffer).strip()
    if len(joined) < _MIN_USABLE_CHARS and not had_hard_break:
        return None, had_hard_break
    return _Chunk(text=joined, had_hard_break=had_hard_break), had_hard_break


def is_markdown_heading(line: str) -> bool:
    return bool(_HEADING_PREFIX.match(line))
