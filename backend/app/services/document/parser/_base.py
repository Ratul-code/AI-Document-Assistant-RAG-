from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.models.document_page import TextSource


class EmptyDocumentError(ValueError):
    """Raised when a document cannot produce any useful page content."""


@dataclass(frozen=True)
class ParsedPage:
    """Parser output that maps directly to a DocumentPage row."""

    page_number: int
    raw_text: str | None = None
    ocr_text: str | None = None
    final_text: str = ""
    text_source: TextSource = TextSource.raw
    parser_name: str | None = None
    parser_version: str | None = None
    ocr_engine: str | None = None
    ocr_confidence: float | None = None
    page_metadata: dict[str, Any] = field(default_factory=dict)


class Parser(Protocol):
    name: str
    version: str

    def parse(self, file_path: Path, *, filename: str) -> list[ParsedPage]: ...
