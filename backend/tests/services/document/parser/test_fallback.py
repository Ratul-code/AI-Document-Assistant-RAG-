from __future__ import annotations

from pathlib import Path

import pytest

from app.services.document.parser import parse_document
from app.services.document.parser._base import EmptyDocumentError


def test_fallback_used_for_unknown_mime(tmp_path: Path) -> None:
    path = tmp_path / "weird.dat"
    path.write_text(
        "This is a fallback document with enough text to parse cleanly.",
        encoding="utf-8",
    )
    pages = parse_document(path, "application/x-unknown", "weird.dat")
    assert len(pages) == 1
    assert pages[0].page_metadata.get("fallback") is True


def test_fallback_raises_empty_for_unreadable_file(tmp_path: Path) -> None:
    path = tmp_path / "binary.dat"
    path.write_bytes(b"\x00\x01\x02\x03\x04")
    with pytest.raises(EmptyDocumentError):
        parse_document(path, "application/x-unknown", "binary.dat")
