"""Track the active heading hierarchy while walking page text.

Each page is split into paragraphs (or any "block" the parser emits).
For every block we resolve which headings are currently in scope and
emit a `section_path` — the list of heading strings from the outermost
down to the innermost.

PDF pages emit no headings (parser decision) so the tracker leaves
`section_path` empty for them.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SectionTracker:
    """Mutable state machine over a single page's headings."""

    _stack: list[list[str]] = field(default_factory=list)

    def update(self, headings: list[dict]) -> None:
        """Apply a batch of `{level, text}` headings from one page block.

        Levels: 1..3 (anything else is ignored). A higher level pops the
        stack down to that level before pushing the new heading, so the
        hierarchy stays consistent.
        """
        for heading in headings:
            level = heading.get("level")
            text = (heading.get("text") or "").strip()
            if not text or not isinstance(level, int) or level < 1 or level > 3:
                continue
            # Trim deeper-or-equal levels so the stack only has
            # strictly shallower levels beneath the new one.
            while len(self._stack) >= level:
                self._stack.pop()
            # Pad if the new level skipped some (e.g. H1 → H3).
            while len(self._stack) < level - 1:
                self._stack.append([""])
            self._stack.append([text])

    def path(self) -> list[str]:
        """Return the current section path (immutable copy)."""
        return [slot[0] for slot in self._stack if slot[0]]


__all__ = ["SectionTracker"]
