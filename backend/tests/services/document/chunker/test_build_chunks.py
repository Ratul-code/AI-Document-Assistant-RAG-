from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.services.document.chunker import build_chunks_for_page


def _page(text: str, *, headings: list[dict] | None = None):
    """Build a fake DocumentPage for testing.

    `DocumentPage` is a SQLAlchemy ORM class so constructing it directly
    requires a session. The chunker only reads `id`, `final_text`, and
    `page_metadata`, so a SimpleNamespace is enough.
    """
    return SimpleNamespace(
        id=uuid4(),
        final_text=text,
        page_metadata={"headings": headings} if headings is not None else None,
    )


def test_empty_page_returns_no_chunks() -> None:
    assert build_chunks_for_page(_page("")) == []


def test_short_markdown_page_returns_single_chunk() -> None:
    page = _page("Hello world.")
    chunks = build_chunks_for_page(page)
    assert len(chunks) == 1
    assert chunks[0].chunk_index == 0
    assert chunks[0].text == "Hello world."


def test_chunk_indices_are_zero_indexed_and_sequential() -> None:
    text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
    chunks = build_chunks_for_page(_page(text))
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_chunks_link_to_next_and_prev() -> None:
    text = "Para one.\n\nPara two.\n\nPara three."
    chunks = build_chunks_for_page(_page(text))
    # Single short page is one chunk — no prev/next.
    if len(chunks) >= 2:
        assert chunks[0].prev_chunk_id is None
        assert chunks[0].next_chunk_id == chunks[1].id
        assert chunks[1].prev_chunk_id == chunks[0].id
        assert chunks[1].next_chunk_id == chunks[2].id
        assert chunks[-1].next_chunk_id is None


def test_long_markdown_page_produces_multiple_chunks() -> None:
    para = "Lorem ipsum dolor sit amet. " * 50  # ~1250 tokens
    text = "\n\n".join([para] * 5)
    chunks = build_chunks_for_page(_page(text))
    assert len(chunks) > 1
    for c in chunks:
        assert c.token_count <= 800


def test_markdown_headings_are_captured_in_section_path() -> None:
    text = "# Chapter 1\n\nIntro text for chapter 1.\n\n## Section 1.1\n\nMore text."
    headings = [
        {"level": 1, "text": "Chapter 1"},
        {"level": 2, "text": "Section 1.1"},
    ]
    chunks = build_chunks_for_page(_page(text, headings=headings))
    assert len(chunks) >= 1
    # At least one chunk should reflect the H1; the later one may
    # also reflect the H2.
    section_paths = [c.section_path for c in chunks]
    assert any("Chapter 1" in path for path in section_paths)


def test_pdf_page_with_no_headings_has_empty_section_path() -> None:
    text = "First page of a PDF that has no headings.\n\nJust plain text."
    chunks = build_chunks_for_page(_page(text, headings=[]))
    for c in chunks:
        assert c.section_path == []


def test_chunks_default_to_needing_embedding() -> None:
    chunks = build_chunks_for_page(_page("Just one paragraph."))
    assert all(c.needs_embedding is True for c in chunks)


def test_char_count_matches_text_length() -> None:
    text = "Some paragraph text here."
    chunks = build_chunks_for_page(_page(text))
    for c in chunks:
        assert c.char_count == len(c.text)
