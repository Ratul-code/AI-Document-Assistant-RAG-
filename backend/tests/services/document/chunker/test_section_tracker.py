from __future__ import annotations

from app.services.document.chunker.section_tracker import SectionTracker


def test_empty_tracker_returns_empty_path() -> None:
    assert SectionTracker().path() == []


def test_single_h1_populates_path() -> None:
    tracker = SectionTracker()
    tracker.update([{"level": 1, "text": "Intro"}])
    assert tracker.path() == ["Intro"]


def test_nested_headings_build_path() -> None:
    tracker = SectionTracker()
    tracker.update(
        [
            {"level": 1, "text": "Chapter 1"},
            {"level": 2, "text": "Section 1.1"},
            {"level": 3, "text": "Subsection 1.1.1"},
        ]
    )
    assert tracker.path() == ["Chapter 1", "Section 1.1", "Subsection 1.1.1"]


def test_higher_level_pops_stack() -> None:
    """Going from H3 back up to H1 should clear the deeper entries."""
    tracker = SectionTracker()
    tracker.update(
        [
            {"level": 1, "text": "Chapter 1"},
            {"level": 2, "text": "Section 1.1"},
            {"level": 3, "text": "Subsection 1.1.1"},
        ]
    )
    tracker.update([{"level": 1, "text": "Chapter 2"}])
    assert tracker.path() == ["Chapter 2"]


def test_same_level_replaces() -> None:
    tracker = SectionTracker()
    tracker.update(
        [
            {"level": 1, "text": "Chapter 1"},
            {"level": 2, "text": "Section 1.1"},
        ]
    )
    tracker.update([{"level": 2, "text": "Section 1.2"}])
    assert tracker.path() == ["Chapter 1", "Section 1.2"]


def test_skipping_a_level_pads_with_empties() -> None:
    """H1 → H3 should leave the H2 slot empty (filtered out by path())."""
    tracker = SectionTracker()
    tracker.update(
        [
            {"level": 1, "text": "Chapter 1"},
            {"level": 3, "text": "Deep dive"},
        ]
    )
    # The empty H2 slot is filtered out by path().
    assert tracker.path() == ["Chapter 1", "Deep dive"]


def test_levels_outside_1_to_3_are_ignored() -> None:
    tracker = SectionTracker()
    tracker.update(
        [
            {"level": 0, "text": "Nope"},
            {"level": 4, "text": "Also nope"},
            {"level": 1, "text": "Real heading"},
        ]
    )
    assert tracker.path() == ["Real heading"]


def test_empty_text_heading_is_ignored() -> None:
    tracker = SectionTracker()
    tracker.update(
        [
            {"level": 1, "text": ""},
            {"level": 1, "text": "  "},
            {"level": 1, "text": "Real"},
        ]
    )
    assert tracker.path() == ["Real"]


def test_pdf_page_with_no_headings_keeps_empty_path() -> None:
    tracker = SectionTracker()
    tracker.update([])
    assert tracker.path() == []
