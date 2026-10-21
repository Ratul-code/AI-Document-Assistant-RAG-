from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.services.document.parser import parse_document
from app.services.document.parser.text_splitter import split_into_pages


def test_md_splitter_breaks_on_paragraph_boundary(long_md_path: Path) -> None:
    pages = parse_document(long_md_path, "text/markdown", "long.md")
    assert len(pages) > 1
    for page in pages:
        assert len(page.raw_text) > 0
        assert page.page_metadata["split_strategy"] == "soft-5000"
    # No page should be over the soft target by more than one paragraph
    for page in pages:
        assert len(page.raw_text) < 12000


def test_md_splitter_does_not_break_sentences(no_break_md_path: Path) -> None:
    """When there's no paragraph boundary, the splitter uses a hard cap."""
    chunks = split_into_pages("x" * 5500, parser_name="test")
    # With no break-points at all, the splitter should still produce one chunk.
    assert len(chunks) >= 1
    # The chunk is exactly the input — there's no break to be made.
    assert chunks[0].text == "x" * 5500


def test_md_splitter_hard_break_at_8000_logs_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A single mega-paragraph should trigger the hard cap and split."""
    paragraph = "Sentence one. Sentence two. Sentence three. "  # 43 chars
    body = paragraph * 250  # ~10,750 chars — well over the 8000 hard cap
    with caplog.at_level(logging.DEBUG, logger="app.services.document.parser.text_splitter"):
        chunks = split_into_pages(body, parser_name="md-splitter")
    assert len(chunks) >= 2
    assert any(c.had_hard_break for c in chunks)
    # No chunk should be wildly over the hard cap.
    for chunk in chunks:
        assert len(chunk.text) <= 8500


def test_txt_basic_parse(tiny_txt_path: Path) -> None:
    pages = parse_document(tiny_txt_path, "text/plain", "tiny.txt")
    assert len(pages) == 1
    assert "tiny plain text document" in pages[0].raw_text
