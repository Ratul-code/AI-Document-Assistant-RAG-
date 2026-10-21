"""Shared fixtures for the parser tests.

PDFs are generated in-memory via reportlab (production-grade, available
in requirements). DOCX is built via python-docx itself. MD / TXT are raw
strings. Each fixture returns a `Path` to a tempfile that the test is
responsible for cleaning up.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from docx import Document as DocxDocument
from reportlab.pdfgen import canvas


def _write_tmp(tmp_path: Path, suffix: str, data: bytes) -> Path:
    p = tmp_path / f"fixture{suffix}"
    p.write_bytes(data)
    return p


@pytest.fixture
def tiny_pdf_bytes() -> bytes:
    """Two-page PDF with a few lines of text on each page."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for _ in range(2):
        c.drawString(72, 720, "This is page one of the test document.")
        c.drawString(72, 700, "It contains enough text to parse cleanly.")
        c.drawString(72, 680, "Line three of the page.")
        c.showPage()
    c.save()
    return buf.getvalue()


@pytest.fixture
def tiny_pdf_path(tmp_path: Path, tiny_pdf_bytes: bytes) -> Path:
    return _write_tmp(tmp_path, ".pdf", tiny_pdf_bytes)


@pytest.fixture
def tiny_docx_bytes() -> bytes:
    """In-memory DOCX with three Heading 1 sections to exercise splitting."""
    doc = DocxDocument()
    doc.add_heading("Introduction", level=1)
    doc.add_paragraph("This is the body of the introduction section.")
    doc.add_paragraph("It has multiple paragraphs to make it non-trivial.")
    doc.add_heading("Methods", level=1)
    doc.add_paragraph("Body of the methods section, with enough text.")
    doc.add_heading("Results", level=1)
    doc.add_paragraph("Body of the results section, also with enough text.")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


@pytest.fixture
def tiny_docx_path(tmp_path: Path, tiny_docx_bytes: bytes) -> Path:
    return _write_tmp(tmp_path, ".docx", tiny_docx_bytes)


@pytest.fixture
def flat_docx_path(tmp_path: Path) -> Path:
    """A DOCX with no Heading 1 — should collapse to a single page."""
    doc = DocxDocument()
    doc.add_paragraph("Plain first paragraph.")
    doc.add_paragraph("Plain second paragraph.")
    doc.add_paragraph("Plain third paragraph.")
    path = tmp_path / "flat.docx"
    doc.save(str(path))
    return path


@pytest.fixture
def table_docx_path(tmp_path: Path) -> Path:
    """A DOCX with a 3x2 table and surrounding paragraphs."""
    doc = DocxDocument()
    doc.add_paragraph("Intro paragraph above the table.")
    table = doc.add_table(rows=3, cols=2)
    table.cell(0, 0).text = "Name"
    table.cell(0, 1).text = "Value"
    table.cell(1, 0).text = "alpha"
    table.cell(1, 1).text = "1"
    table.cell(2, 0).text = "beta"
    table.cell(2, 1).text = "2"
    doc.add_paragraph("Outro paragraph below the table.")
    path = tmp_path / "table.docx"
    doc.save(str(path))
    return path


@pytest.fixture
def tiny_md_bytes() -> bytes:
    return (
        b"# Title\n\n"
        b"This is the first paragraph of the markdown document.\n\n"
        b"This is another paragraph with enough text.\n\n"
        b"Final paragraph here, also long enough to count.\n"
    )


@pytest.fixture
def tiny_md_path(tmp_path: Path, tiny_md_bytes: bytes) -> Path:
    return _write_tmp(tmp_path, ".md", tiny_md_bytes)


@pytest.fixture
def tiny_txt_path(tmp_path: Path) -> Path:
    path = tmp_path / "tiny.txt"
    path.write_text(
        "This is a tiny plain text document.\n"
        "It has multiple lines and enough text to be parsed.\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def long_md_path(tmp_path: Path) -> Path:
    """Markdown with multiple paragraphs summing to >5000 chars."""
    paragraphs = [
        (
            "This is a long paragraph meant to push the splitter over the "
            "five-thousand-character target so it has to actually split. " * 5
        ).strip()
        for _ in range(20)
    ]
    path = tmp_path / "long.md"
    path.write_text("\n\n".join(paragraphs), encoding="utf-8")
    return path


@pytest.fixture
def no_break_md_path(tmp_path: Path) -> Path:
    """5500 chars of unbroken text — exercises the hard-cap fallback."""
    body = "x" * 5500
    path = tmp_path / "no_break.md"
    path.write_text(body, encoding="utf-8")
    return path
