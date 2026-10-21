from __future__ import annotations

from pathlib import Path

from app.models.document_page import TextSource

from ._base import EmptyDocumentError, ParsedPage

PARSER_NAME = "text-fallback"
PARSER_VERSION = "1.0.0"


class FallbackParser:
    """Last-resort parser used when MIME / extension are unknown.

    Tries to decode the file as UTF-8. If the result is below the usable
    threshold or cannot be decoded, raises EmptyDocumentError so the
    worker deletes the row.
    """

    name = PARSER_NAME
    version = PARSER_VERSION

    def parse(self, file_path: Path, *, filename: str) -> list[ParsedPage]:
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise EmptyDocumentError(f"cannot read file: {exc}") from exc

        stripped = text.strip()
        if len(stripped) < 20:
            raise EmptyDocumentError("file produced no usable text")

        return [
            ParsedPage(
                page_number=1,
                raw_text=stripped,
                text_source=TextSource.raw,
                parser_name=self.name,
                parser_version=self.version,
                page_metadata={"fallback": True, "char_count": len(stripped)},
            )
        ]
