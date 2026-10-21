from __future__ import annotations

from app.models.document_page import TextSource

from ._base import EmptyDocumentError, ParsedPage

_MIN_USABLE_CHARS = 20


def renumber_and_finalize(
    pages: list[ParsedPage],
    *,
    parser_name: str,
    parser_version: str,
) -> list[ParsedPage]:
    """Normalize parser output before it is persisted."""
    if not pages:
        raise EmptyDocumentError("document produced no pages")

    finalized: list[ParsedPage] = []
    for page_number, page in enumerate(pages, start=1):
        raw_text = page.raw_text.strip() if page.raw_text else None
        ocr_text = page.ocr_text.strip() if page.ocr_text else None
        final_text = raw_text or ocr_text or ""
        metadata = dict(page.page_metadata or {})

        if raw_text is None and ocr_text is None:
            metadata.setdefault("needs_ocr", True)
            text_source = TextSource.ocr
        elif raw_text and ocr_text:
            text_source = TextSource.merged
            final_text = f"{raw_text}\n\n{ocr_text}"
        elif raw_text:
            text_source = TextSource.raw
        else:
            text_source = TextSource.ocr

        finalized.append(
            ParsedPage(
                page_number=page_number,
                raw_text=raw_text,
                ocr_text=ocr_text,
                final_text=final_text,
                text_source=text_source,
                parser_name=page.parser_name or parser_name,
                parser_version=page.parser_version or parser_version,
                ocr_engine=page.ocr_engine,
                ocr_confidence=page.ocr_confidence,
                page_metadata=metadata,
            )
        )

    if all(len(page.final_text.strip()) < _MIN_USABLE_CHARS for page in finalized):
        # Keep pages that are explicitly marked for future OCR. A document
        # with no useful text and no OCR candidates is genuinely empty.
        if not any(page.page_metadata.get("needs_ocr") for page in finalized):
            raise EmptyDocumentError("document produced no usable text")

    return finalized
