from __future__ import annotations

from pathlib import Path

from docx import Document as DocxDocument
from docx.document import Document as _DocxDoc
from docx.table import Table as _DocxTable

from app.models.document_page import TextSource

from ._base import EmptyDocumentError, ParsedPage

PARSER_NAME = "python-docx"
PARSER_VERSION = "1.x"

try:
    import docx as _docx_module

    PARSER_VERSION = getattr(_docx_module, "__version__", PARSER_VERSION)
except Exception:  # pragma: no cover
    pass


class DocxParser:
    name = PARSER_NAME
    version = PARSER_VERSION

    def parse(self, file_path: Path, *, filename: str) -> list[ParsedPage]:
        try:
            document: _DocxDoc = DocxDocument(str(file_path))
        except Exception as exc:
            raise EmptyDocumentError(f"cannot read DOCX: {exc}") from exc

        # Each "page" is a list of "blocks". A block is either:
        #   ("p", "paragraph text")            - a normal paragraph
        #   ("table", "markdown table text")   - a rendered table
        # Each entry is (page_blocks, page_headings). We track headings
        # alongside blocks so the chunker can use them as section anchors.
        pages: list[tuple[list[tuple[str, str]], list[dict]]] = [([], [])]
        paragraph_count = 0
        table_count = 0

        for kind, content in _iter_block_items(document):
            if kind == "p":
                text = content.text.strip()
                if not text:
                    continue
                paragraph_count += 1

                style_name = (
                    (content.style.name or "").lower() if getattr(content, "style", None) else ""
                )
                # Strict match — "heading 10" must not match.
                if style_name in {"heading 1", "title"}:
                    if pages[-1][0]:
                        pages.append(([], []))
                    pages[-1][0].append(("p", text))
                    pages[-1][1].append({"level": 1, "text": text})
                    continue

                level = _heading_level(style_name)
                if level is not None:
                    pages[-1][0].append(("p", text))
                    pages[-1][1].append({"level": level, "text": text})
                    continue

                pages[-1][0].append(("p", text))

            elif kind == "table":
                table: _DocxTable = content
                markdown = _render_table_as_markdown(table)
                if not markdown.strip():
                    continue
                table_count += 1
                pages[-1][0].append(("table", markdown))

        if paragraph_count == 0 and table_count == 0:
            raise EmptyDocumentError("DOCX has no paragraphs or tables")

        split_strategy = "heading1" if len(pages) > 1 else "single"

        parsed: list[ParsedPage] = []
        for idx, (page_blocks, page_headings) in enumerate(pages, start=1):
            joined = "\n\n".join(block_text for _, block_text in page_blocks).strip()
            if not joined:
                continue

            needs_ocr = len(joined) < 20
            parsed.append(
                ParsedPage(
                    page_number=idx,
                    raw_text=None if needs_ocr else joined,
                    text_source=TextSource.ocr if needs_ocr else TextSource.raw,
                    parser_name=None if needs_ocr else self.name,
                    parser_version=None if needs_ocr else self.version,
                    page_metadata={
                        "split_strategy": split_strategy,
                        "paragraph_count": paragraph_count,
                        "table_count": table_count,
                        "tables": _collect_table_metadata(page_blocks),
                        "headings": page_headings,
                    },
                )
            )

        if not parsed:
            raise EmptyDocumentError("DOCX produced no pages")

        return parsed


# ---------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------


def _iter_block_items(document: _DocxDoc):
    """Yield (kind, content) tuples in document order.

    Kinds:
        "p"     -> a Paragraph object
        "table" -> a Table object

    Tables are yielded as a whole block (not flattened) so the parser
    can render them as Markdown instead of merging cells.
    """
    from docx.oxml.ns import qn

    paragraph_by_xml = {p._p: p for p in document.paragraphs}
    table_by_xml = {t._tbl: t for t in document.tables}

    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            paragraph = paragraph_by_xml.get(child)
            if paragraph is not None:
                yield "p", paragraph
        elif child.tag == qn("w:tbl"):
            table = table_by_xml.get(child)
            if table is not None:
                yield "table", table


def _render_table_as_markdown(table: _DocxTable) -> str:
    """Render a DOCX table as a GFM (GitHub-Flavored Markdown) table.

    Empty cells become empty strings; rows with no text are skipped so
    we don't emit half-blank rows that confuse downstream parsers.
    """
    rows: list[list[str]] = []
    col_count = 0
    for row in table.rows:
        cells = [cell.text.strip().replace("\n", " ").replace("|", "\\|") for cell in row.cells]
        if not any(cells):
            continue
        rows.append(cells)
        col_count = max(col_count, len(cells))

    if not rows:
        return ""

    # Pad short rows so the column count is uniform.
    for cells in rows:
        while len(cells) < col_count:
            cells.append("")

    # First row becomes the header.
    header, *body = rows
    lines = ["| " + " | ".join(header) + " |"]
    lines.append("| " + " | ".join(["---"] * col_count) + " |")
    for row in body:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _collect_table_metadata(
    page_blocks: list[tuple[str, str]],
) -> list[dict]:
    """Extract per-table metadata for storage in page_metadata.

    Kept lightweight — row/col counts and the rendered Markdown. Future
    endpoints can return these for non-LLM consumers.
    """
    metadata: list[dict] = []
    for kind, content in page_blocks:
        if kind != "table":
            continue
        # Count rows by splitting on newlines; first line is the header,
        # second is the separator, the rest are body rows.
        lines = content.split("\n")
        header_row = lines[0] if lines else ""
        body_rows = lines[2:] if len(lines) > 2 else []
        col_count = max(0, header_row.count("|") - 1)
        metadata.append(
            {
                "row_count": 1 + len(body_rows),  # header + body
                "col_count": col_count,
                "markdown": content,
            }
        )
    return metadata


def _heading_level(style_name: str) -> int | None:
    """Map a paragraph style name to a heading level, or None.

    Accepts the canonical "heading 1" / "heading 2" / "heading 3" styles
    plus their localized variants ("heading 1", "titulo 1", etc.) by
    matching a trailing integer. Returns None for body / title styles.
    """
    if not style_name:
        return None
    parts = style_name.split()
    if len(parts) < 2:
        return None
    if parts[0] != "heading":
        return None
    try:
        level = int(parts[1])
    except ValueError:
        return None
    if 1 <= level <= 3:
        return level
    return None
