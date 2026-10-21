from __future__ import annotations

import re
from pathlib import Path

from app.models.document_page import TextSource

from ._base import ParsedPage
from .text_splitter import split_into_pages

PARSER_NAME = "txt-splitter"
PARSER_VERSION = "1.0.0"

_NUMBERED_PREFIX_RE = re.compile(r"^(\d+(?:\.\d+){0,2})\.?\s+(.+?)\s*$")
_ALLCAPS_LINE_RE = re.compile(r"^[A-Z0-9 ,.;:'-]{3,80}$")


class TxtParser:
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
                    "headings": _extract_txt_headings(chunk.text),
                },
            )
            for idx, chunk in enumerate(chunks)
        ]


def _extract_txt_headings(text: str) -> list[dict]:
    """Heuristic heading detection for plain text.

    Two rules:
      1. Lines matching `1.`, `1.1`, `1.1.1` prefixes become headings.
         The depth (number of dots) sets the level.
      2. ALL-CAPS lines (<= 80 chars) surrounded by blank lines are
         treated as level-1 headings.

    Returns at most one heading per line. Order preserved.
    """
    headings: list[dict] = []
    lines = text.split("\n")

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        # Numbered prefix.
        match = _NUMBERED_PREFIX_RE.match(stripped)
        if match:
            depth = match.group(1).count(".")
            headings.append({"level": min(depth + 1, 3), "text": match.group(2)})
            continue

        # ALL CAPS with surrounding blank lines.
        if _ALLCAPS_LINE_RE.match(stripped) and stripped == stripped.upper():
            prev_blank = idx == 0 or not lines[idx - 1].strip()
            next_blank = idx == len(lines) - 1 or not lines[idx + 1].strip()
            if prev_blank and next_blank:
                headings.append({"level": 1, "text": stripped})
    return headings
