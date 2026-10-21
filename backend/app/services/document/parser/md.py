from __future__ import annotations

import re
from pathlib import Path

from app.models.document_page import TextSource

from ._base import ParsedPage
from .text_splitter import split_into_pages

PARSER_NAME = "md-splitter"
PARSER_VERSION = "1.0.0"

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$", re.MULTILINE)


class MarkdownParser:
    name = PARSER_NAME
    version = PARSER_VERSION

    def parse(self, file_path: Path, *, filename: str) -> list[ParsedPage]:
        text = file_path.read_text(encoding="utf-8", errors="replace")
        chunks = split_into_pages(text, parser_name=self.name)
        return [
            ParsedPage(
                page_number=idx + 1,
                raw_text=chunk.text,
                text_source=TextSource.raw,
                parser_name=self.name,
                parser_version=self.version,
                page_metadata={
                    "split_strategy": "soft-5000",
                    "hard_break": chunk.had_hard_break,
                    "char_count": len(chunk.text),
                    "headings": _extract_markdown_headings(chunk.text),
                },
            )
            for idx, chunk in enumerate(chunks)
        ]


def _extract_markdown_headings(text: str) -> list[dict]:
    """Find `#`, `##`, `###` lines. Levels 4+ are ignored."""
    return [
        {"level": len(match.group(1)), "text": match.group(2)}
        for match in _HEADING_RE.finditer(text)
    ]
