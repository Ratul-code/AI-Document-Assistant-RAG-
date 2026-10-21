"""Build chunk rows from a parsed page.

This module glues together:
  - the recursive token-aware splitter
  - the section tracker (heading hierarchy)
  - the page model + page_metadata["headings"]

It produces `ChunkSpec` rows ready for the chunk repository to persist.

The page is split into paragraphs (blank-line separated). Each
paragraph is checked against the page's heading list; matching a
heading advances the section tracker and the heading itself stays as
its own block so it isn't lost from the chunk text. All blocks are
then joined and run through the splitter once. Each emitted chunk's
section_path is determined by the first block whose text appears in
the chunk — a deterministic, O(chunks × blocks) lookup that is cheap
because the block list is small (one entry per paragraph).
"""

from __future__ import annotations

from uuid import UUID, uuid4

from app.models.document_page import DocumentPage
from app.repositories.document_chunk import ChunkSpec

from .section_tracker import SectionTracker
from . import split_text


def build_chunks_for_page(page: DocumentPage) -> list[ChunkSpec]:
    """Convert a persisted `DocumentPage` into chunk rows.

    Returns an empty list if the page has no extractable text. PDF pages
    (no headings) end up with `section_path == []`.
    """
    text = (page.final_text or "").strip()
    if not text:
        return []

    headings = _extract_headings(page)

    tracker = SectionTracker()
    # Precompute normalized heading text once. Dict lookup avoids an
    # O(headings × paragraphs) inner loop.
    pending_headings: dict[str, dict] = {
        _normalize(h.get("text", "")): h
        for h in headings
        if _normalize(h.get("text", ""))
    }

    paragraphs = _split_into_paragraphs(text)
    blocks: list[str] = []
    paths: list[list[str]] = []

    for paragraph in paragraphs:
        # Strip leading markdown heading markers so `# Heading` matches
        # a heading entry whose `text` is `Heading`.
        normalized = _normalize(_strip_md_heading(paragraph))
        heading = pending_headings.get(normalized)
        if heading is not None:
            tracker.update([{"level": int(heading["level"]), "text": heading.get("text", "")}])
            del pending_headings[normalized]
        blocks.append(paragraph)
        paths.append(list(tracker.path()))

    if not blocks:
        return []

    joined = "\n\n".join(blocks)
    text_chunks = split_text(joined)
    if not text_chunks:
        return []

    # Precompute normalized block prefixes once for the chunk → section
    # lookup. Truncation is generous enough to disambiguate paragraphs
    # without matching unrelated content.
    block_prefixes = [_normalize(b)[:80] for b in blocks]

    chunks: list[ChunkSpec] = []
    prev_id: UUID | None = None
    for chunk in text_chunks:
        chunk_id = uuid4()
        path = _best_path_for_chunk(chunk.text, paths, block_prefixes)
        chunks.append(
            ChunkSpec(
                id=chunk_id,
                page_id=page.id,
                chunk_index=len(chunks),
                section_path=path,
                text=chunk.text,
                token_count=chunk.token_count,
                char_count=len(chunk.text),
                prev_chunk_id=prev_id,
                next_chunk_id=None,
            )
        )
        prev_id = chunk_id

    # Wire next_chunk_id — single pass, no mutation tricks needed.
    for idx in range(len(chunks) - 1):
        chunks[idx].next_chunk_id = chunks[idx + 1].id

    return chunks


# ---------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------


def _extract_headings(page: DocumentPage) -> list[dict]:
    """Pull the heading list out of `page.page_metadata`, defensively."""
    if not page.page_metadata:
        return []
    raw = page.page_metadata.get("headings")
    if not isinstance(raw, list):
        return []
    return [
        h for h in raw
        if isinstance(h, dict) and isinstance(h.get("level"), int)
    ]


def _split_into_paragraphs(text: str) -> list[str]:
    """Split `text` on blank-line boundaries; keep order."""
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def _normalize(text: str) -> str:
    """Lowercase, collapse whitespace, strip — used for fuzzy matching."""
    return " ".join(text.lower().split())


def _strip_md_heading(text: str) -> str:
    """Drop leading `#` markers from a markdown heading line."""
    return text.lstrip().lstrip("#").strip()


def _best_path_for_chunk(
    chunk_text: str,
    paths: list[list[str]],
    block_prefixes: list[str],
) -> list[str]:
    """Pick the section_path for `chunk_text`.

    Strategy: find the first block whose normalized prefix appears at
    the start of `chunk_text`. If none match, return the last known
    path — the chunk almost certainly trails into a later section.
    """
    snippet = chunk_text.lstrip().split("\n\n", 1)[0].strip()
    snippet_norm = _normalize(snippet)[:80]
    if snippet_norm:
        for prefix, path in zip(block_prefixes, paths):
            if prefix and snippet_norm in prefix:
                return list(path)
    return list(paths[-1]) if paths else []


__all__ = ["build_chunks_for_page"]
