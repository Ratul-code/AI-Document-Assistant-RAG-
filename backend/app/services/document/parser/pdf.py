from __future__ import annotations

import logging
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.models.document_page import TextSource

from ._base import EmptyDocumentError, ParsedPage

logger = logging.getLogger(__name__)

PARSER_NAME = "pypdf"
PARSER_VERSION = "5.x"

try:
    import pypdf as _pypdf_module

    PARSER_VERSION = getattr(_pypdf_module, "__version__", PARSER_VERSION)
except Exception:  # pragma: no cover
    pass


class PdfParser:
    name = PARSER_NAME
    version = PARSER_VERSION

    def parse(self, file_path: Path, *, filename: str) -> list[ParsedPage]:
        try:
            reader = PdfReader(str(file_path))
            pages = list(reader.pages)
        except (PdfReadError, OSError) as exc:
            raise EmptyDocumentError(f"cannot read PDF: {exc}") from exc

        if not pages:
            raise EmptyDocumentError("PDF has zero pages")

        parsed: list[ParsedPage] = []
        for index, page in enumerate(pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception as exc:
                logger.warning(
                    "pdf parser: extract_text failed page=%d err=%s", index, exc,
                )
                text = ""

            if len(text.strip()) < 20:
                parsed.append(
                    ParsedPage(
                        page_number=index,
                        raw_text=None,
                        text_source=TextSource.ocr,
                        parser_name=None,
                        page_metadata={
                            "needs_ocr": True,
                            "pdf_page_index": index,
                            "raw_text_len": len(text.strip()),
                            "headings": [],
                        },
                    )
                )
            else:
                parsed.append(
                    ParsedPage(
                        page_number=index,
                        raw_text=text,
                        text_source=TextSource.raw,
                        parser_name=self.name,
                        parser_version=self.version,
                        page_metadata={"pdf_page_index": index, "headings": []},
                    )
                )

        return parsed
