"""Document parsers.

Each parser turns a file into a list of `ParsedPage` objects. The
worker calls `parse_document(file_path, mime_type, filename)` and
gets back the finalized pages ready to persist.

Routing order:
1. exact MIME match in `PARSERS_BY_MIME`
2. extension match in `PARSERS_BY_EXT`
3. `FALLBACK_PARSER` (text fallback)

OCR is not implemented in this round — every parser returns
`text_source='ocr'` rows for pages where it found no usable text,
flagging them via `page_metadata['needs_ocr']`. The future OCR
worker reads those rows and back-fills `ocr_text`.
"""

from __future__ import annotations

from pathlib import Path

from ._base import EmptyDocumentError, ParsedPage, Parser
from ._finalize import renumber_and_finalize
from .docx import DocxParser
from .fallback import FallbackParser
from .md import MarkdownParser
from .pdf import PdfParser
from .txt import TxtParser

PARSERS_BY_MIME: dict[str, Parser] = {
    "application/pdf": PdfParser(),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DocxParser(),
    "text/markdown": MarkdownParser(),
    "text/plain": TxtParser(),
}

PARSERS_BY_EXT: dict[str, Parser] = {
    ".pdf": PARSERS_BY_MIME["application/pdf"],
    ".docx": PARSERS_BY_MIME["application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
    ".md": PARSERS_BY_MIME["text/markdown"],
    ".txt": PARSERS_BY_MIME["text/plain"],
}

FALLBACK_PARSER: Parser = FallbackParser()


def parse_document(
    file_path: Path, mime_type: str, filename: str
) -> list[ParsedPage]:
    """Pick the right parser and return finalized pages."""
    parser: Parser = (
        PARSERS_BY_MIME.get(mime_type)
        or PARSERS_BY_EXT.get(Path(filename).suffix.lower())
        or FALLBACK_PARSER
    )
    pages = parser.parse(file_path, filename=filename)
    return renumber_and_finalize(
        pages, parser_name=parser.name, parser_version=parser.version
    )


__all__ = [
    "EmptyDocumentError",
    "FALLBACK_PARSER",
    "PARSERS_BY_EXT",
    "PARSERS_BY_MIME",
    "ParsedPage",
    "Parser",
    "parse_document",
]
