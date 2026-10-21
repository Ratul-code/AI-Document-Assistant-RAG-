from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from app.models.document_page import TextSource
from app.services.document.parser import parse_document
from app.services.document.parser._base import EmptyDocumentError


def test_pdf_returns_one_page_per_pdf_page(tiny_pdf_path: Path) -> None:
    pages = parse_document(tiny_pdf_path, "application/pdf", "tiny.pdf")
    assert len(pages) == 2
    for page in pages:
        assert page.raw_text is not None
        assert len(page.raw_text.strip()) >= 20
        assert page.text_source == TextSource.raw
        assert page.parser_name == "pypdf"
        assert page.page_metadata.get("pdf_page_index") is not None


def test_pdf_scanned_page_marks_needs_ocr(tiny_pdf_path: Path) -> None:
    """If pypdf returns empty text, the page is flagged for OCR."""
    with patch("pypdf.PageObject.extract_text", return_value=""):
        pages = parse_document(tiny_pdf_path, "application/pdf", "tiny.pdf")
    assert len(pages) == 2
    for page in pages:
        assert page.raw_text is None
        assert page.text_source == TextSource.ocr
        assert page.page_metadata.get("needs_ocr") is True


def test_pdf_unsupported_file_raises_empty(tmp_path: Path) -> None:
    bogus = tmp_path / "bogus.pdf"
    bogus.write_bytes(b"not a real pdf")
    with pytest.raises(EmptyDocumentError):
        parse_document(bogus, "application/pdf", "bogus.pdf")
