"""Prompt construction for the RAG (analysis) pipeline.

The analyser sends a single OpenAI-style ``chat/completions`` request
to the LLM. This module owns every line of that payload except the
hard-wired HTTP/streaming plumbing (which lives in
``app/services/ai/llm.py``).

Three logical sections in the final request:

  1. **System** — ``SYSTEM_PROMPT`` below. Fixed; describes persona,
     grounding rules, and what the prior ``<CHAT_HISTORY>`` block
     is *for*. Stable across requests so we can hot-tune it without
     shipping new code.

  2. **Chat history** — assembled by ``chat_history.fetch_recent_turns``
     and threaded through ``build_messages(history=...)``. Only
     present for conversational callers; the legacy REST ``/query``
     path passes ``history=[]`` and gets the v1 single-user-message
     shape.

  3. **User message** — ``build_user_prompt`` produces this. It
     groups retrieved snippets by document inside a ``<DOC_CONTEXT>``
     block, then writes the current question inside
     ``<CURRENT_QUESTION>``.

The cited-number mapping is the same one we hand to the client in
``QueryCitation.index``. ``[1]`` in the answer body, list item
``index=1`` in the response — they refer to the same chunk. The
numbering stays **global** (not per-document) so the citation map
is 1:1 with the rendered citations, even when multiple documents
are in scope.

Snippet trimming:
    Chunks come out of the retriever at full length (up to 800 tokens
    by the chunker). With several in the prompt we often hit the
    per-snippet cap (``analysis_snippet_chars``). The old code did
    ``text[:n].rstrip() + "…"`` — a hard cut that landed mid-sentence
    and gave the model a partial thought as evidence. The model
    would then over-cite the truncated sentence as ground truth and
    the answer lost context. ``trim_snippet`` (below) replaces that
    with three improvements:

      1. **Sentence-aware boundary.** Find the last sentence
         terminator (``.``, ``!``, ``?`` followed by whitespace, or
         a paragraph break) before the cap and cut there. Falls
         back to the last whitespace if there's no sentence
         boundary in budget.
      2. **Head + tail windowing.** When the chunk is much longer
         than the budget (over 2×), keep the opening AND closing
         portions with an explicit ``…N chars omitted in middle…``
         marker. Answers usually live in the head or tail of a
         passage, not the middle.
      3. **Honest omission marker.** A trailing ``…`` (single
         ellipsis char) on every trimmed chunk so the model knows
         the evidence is partial and won't invent details for the
         dropped portion.

    Both the prompt (``build_user_prompt``) and the citation
    snippet (``_snippet_for_response`` in ``__init__.py``) call
    this helper so the two surfaces stay in lockstep.
"""

from __future__ import annotations

import re
from typing import Sequence
from uuid import UUID

from app.repositories.embedding_search import CandidateRow
from app.services.ai.llm import LLMMessage


SYSTEM_PROMPT = """\
You are a thoughtful document-grounded assistant. Your job is to help \
the user understand their own documents — not to retrieve them.

VOICE & STYLE
- Sound like a knowledgeable colleague explaining over coffee, not \
a search engine. Use natural prose; vary sentence length.
- Match the user's register: a short question deserves a short \
answer; a reflective question can take a paragraph or two.
- Prefer prose with a few well-chosen bullets over dense lists.

GROUNDING RULES
- Use ONLY the numbered snippets inside <DOC_CONTEXT> as your \
evidence. Treat them as your source of truth.
- After every claim that comes from a snippet, append an inline \
citation like [1] or [2] that maps back to the matching number in \
the SOURCES list.
- If a snippet contains multiple sentences, cite once for the \
sentence or claim that uses it — not once per snippet.
- You may briefly rephrase, summarize, or add a one-sentence \
framing of the cited material to make it clearer. Mark any added \
framing with "(my framing)" — never present it as a snippet claim.
- Do not invent facts. If the snippets don't cover the question, \
say so plainly — say you don't know or that the documents do not \
cover the question. Never speculate beyond the provided evidence.
- A snippet marked with "…" is partial — only the shown portion \
is evidence. Do not assume the missing middle or tail contains \
anything specific.
- Mention the document title the first time you cite it in a \
paragraph; afterward just use the number.

CONVERSATION AWARENESS
- Prior turns (if any) appear as a <CHAT_HISTORY> block. Treat \
them as context for the current question — they may clarify terms, \
narrow scope, or carry forward a thread.
- Do NOT cite snippets from the chat history. Only cite the \
numbered SOURCES in <DOC_CONTEXT>.
- If the user is asking a follow-up that depends on a previous \
answer, integrate that — don't re-answer from scratch unless the \
prior answer was wrong.
- Resolve pronouns and references ("the second one", "above", \
"that table", "you mentioned") against <CHAT_HISTORY> before \
answering.
"""


# Sentence terminators: `.`, `!`, `?` followed by whitespace or EOL,
# OR a paragraph boundary. We anchor the regex on the closing punct
# so a cut on `foo.` never bleeds into `foo. bar` (the next sentence).
_SENTENCE_TAIL_RE = re.compile(r"[.!?](?=\s|$)")

# Paragraph break — preferred over sentence boundary because it
# preserves more of the text and almost never splits a thought.
_PARAGRAPH_BREAK_RE = re.compile(r"\n\s*\n")

# Above this multiple of the budget, we switch from "single cut" to
# "head + tail with middle omitted" — answers usually live in one of
# those two regions, and the middle is the most skippable portion.
HEAD_TAIL_RATIO = 2


def _last_boundary(text: str, cap: int) -> int:
    """Return the largest index ≤ cap that's a paragraph break, a
    sentence terminator, or a whitespace. 0 means "no good boundary
    found, caller should hard-cut"."""
    if cap <= 0 or cap >= len(text):
        return min(cap, len(text))

    # Paragraph break is best — preserves the most content. Look in
    # the trailing half of the budget first; if not found, walk back.
    window_start = cap // 2
    head = text[window_start:cap]

    # Prefer paragraph break.
    m = None
    for match in _PARAGRAPH_BREAK_RE.finditer(head):
        m = match
    if m is not None:
        # The break is the end of the matched \n\n; include the
        # position right after the trailing whitespace so the
        # remainder of the text after that is a coherent unit.
        return window_start + m.end()

    # Next: sentence terminator in the last quarter of the budget.
    tail_start = (cap * 3) // 4
    tail = text[tail_start:cap]
    last_sent_end = -1
    for match in _SENTENCE_TAIL_RE.finditer(tail):
        last_sent_end = match.end()
    if last_sent_end != -1:
        return tail_start + last_sent_end

    # Fallback: last whitespace. Avoids cutting mid-word.
    ws_idx = text.rfind(" ", window_start, cap)
    if ws_idx > window_start:
        return ws_idx

    # No good boundary — caller should hard-cut at cap.
    return cap


def trim_snippet(text: str, *, max_chars: int) -> str:
    """Trim a chunk for the prompt without losing meaning.

    Short text (≤ max_chars): returned unchanged after stripping.

    Text longer than max_chars but ≤ 2× max_chars: cut at the last
    paragraph / sentence / word boundary within the budget, with a
    trailing ellipsis so the model sees the trim.

    Text longer than 2× max_chars: take the head half + tail half with
    an explicit ``…N chars omitted in middle…`` marker. This preserves
    the opening context (where the question's subject is usually
    introduced) and the closing argument (where the answer often
    lands) while dropping the most skippable middle.

    The function is total: any int / max_chars limit produces a
    sensible trimmed result, including 0 / negative (returns "").
    """
    if text is None:
        return ""
    s = text.strip()
    if not s:
        return ""
    if max_chars <= 0:
        return ""
    if len(s) <= max_chars:
        return s

    # Head + tail window for very long ones.
    if len(s) > max_chars * HEAD_TAIL_RATIO:
        # Reserve ~30 chars for the omitted-marker line. If the budget
        # is so small that splitting head + tail would leave nothing
        # useful in either half, fall back to a head-only cut with
        # the marker.
        marker_overhead = 30
        min_half = 16  # smallest meaningful half — at least one short sentence
        usable = max_chars - marker_overhead
        if usable < min_half * 2:
            # Budget too tight for head+tail; do a single head cut.
            head_end = _last_boundary(s, max_chars - marker_overhead)
            head = s[: head_end].rstrip() if head_end > 0 else s[: max_chars - marker_overhead]
            omitted = max(0, len(s) - len(head))
            return f"{head} […{omitted} chars omitted…]"

        head_budget = usable // 2
        tail_budget = usable - head_budget
        head_end = _last_boundary(s, head_budget)
        head = s[: head_end].rstrip() if head_end > 0 else s[:head_budget]
        # Tail slice, then snap forward to the next boundary so we
        # never start the tail mid-word.
        tail_start = len(s) - tail_budget
        tail = s[tail_start:]
        # Find the FIRST boundary after tail_start (within the tail
        # window) so the tail starts on a clean word/sentence.
        first_ws = tail.find(" ")
        if first_ws != -1 and first_ws < tail_budget // 4:
            tail_start += first_ws + 1
            tail = s[tail_start:]
        tail_rel = _last_boundary(tail, len(tail))
        tail = tail[: tail_rel].rstrip() if tail_rel > 0 else tail.rstrip()
        omitted = max(1, len(s) - len(head) - len(tail) - 4)
        return (
            f"{head}\n\n[…{omitted} chars omitted in middle…]\n\n{tail}"
        ).rstrip()

    # Plain single cut.
    cut = _last_boundary(s, max_chars - 1)
    if cut <= 0:
        # Couldn't find any — hard-cut.
        return s[: max(0, max_chars - 1)].rstrip() + "…"
    return s[:cut].rstrip() + "…"


def _format_section_path(section_path: Sequence[str]) -> str:
    """Render the heading path as ``§a/b/c``, or ```` if empty."""
    if not section_path:
        return ""
    return "§" + "/".join(section_path)


def _format_snippet(
    *,
    index: int,
    row: CandidateRow,
    snippet_chars: int,
) -> str:
    """Render one numbered snippet.

    Format::

        [N] title — p.M — §a/b/c
        <snippet text>

    The ``title`` is omitted on later snippets from the same document
    to keep the prompt readable. The page number is always shown. The
    section path is shown if non-empty.
    """

    parts = [
        f"[{index}]",
        f"{row.document_title}",
        f"p.{row.page_number + 1}" if row.page_number is not None else "",
        _format_section_path(row.section_path),
    ]
    header = " · ".join(p for p in parts if p)
    text = trim_snippet(row.text or "", max_chars=snippet_chars)
    return f"{header}\n{text}"


def _group_snippets_by_document(
    rows: Sequence[CandidateRow],
) -> list[tuple[str, list[tuple[int, CandidateRow]]]]:
    """Group retrieved rows by document, preserving citation order.

    Returns a list of ``(document_title, [(global_index, row), ...])``
    where ``global_index`` is 1-based across the whole result set (not
    per-group). Document title is taken from the first row of the
    group; later rows in the same group reuse the same title (the
    per-snippet formatter suppresses repeats to keep the prompt
    readable).
    """
    groups: list[tuple[str, list[tuple[int, CandidateRow]]]] = []
    current_title: str | None = None
    current: list[tuple[int, CandidateRow]] = []
    for i, row in enumerate(rows, start=1):
        if row.document_title != current_title:
            if current:
                assert current_title is not None  # for type checker
                groups.append((current_title, current))
            current_title = row.document_title
            current = [(i, row)]
        else:
            current.append((i, row))
    if current:
        assert current_title is not None
        groups.append((current_title, current))
    return groups


def build_user_prompt(
    *,
    question: str,
    top_rows: Sequence[CandidateRow],
    snippet_chars: int,
) -> str:
    """Build the user prompt: grouped snippets, then the question.

    Citations are 1-based and tightly mapped to ``top_rows`` so the
    ``index`` we return in the response matches the ``[N]`` the model
    learned from this prompt.
    """
    if not top_rows:
        # The pre-check upstream should normally turn an empty candidate
        # set into a `NO_DOCUMENTS_READY` error before we get here.
        # This defensive branch keeps the model honest if it's reached.
        return (
            "<DOC_CONTEXT>\n(none)\n</DOC_CONTEXT>\n\n"
            "<CURRENT_QUESTION>\n"
            f"{question}\n"
            "</CURRENT_QUESTION>\n\n"
            "The user has no uploaded documents to search. "
            "Ask them to upload one or expand their selection."
        )

    groups = _group_snippets_by_document(top_rows)
    doc_blocks: list[str] = []
    for _title, items in groups:
        # Per-doc header. The first item in a group carries the full
        # title in its per-snippet header; later items in the same
        # group have the title suppressed by the formatter. We still
        # emit a DOC banner so the model can narrate "in <title>…".
        first_title = items[0][1].document_title
        rendered = [
            _format_snippet(
                index=global_index,
                row=row,
                snippet_chars=snippet_chars,
            )
            for global_index, row in items
        ]
        body = "\n\n---\n\n".join(rendered)
        doc_blocks.append(f"DOC: {first_title}\n{body}")

    doc_context = "\n\n===\n\n".join(doc_blocks)
    numbered = ", ".join(f"[{i + 1}]" for i in range(len(top_rows)))
    return (
        f"<DOC_CONTEXT>\n"
        f"Cite as {numbered}.\n\n{doc_context}\n"
        f"</DOC_CONTEXT>\n\n"
        f"<CURRENT_QUESTION>\n{question}\n</CURRENT_QUESTION>\n\n"
        "Answer using only the sources above. Cite inline. "
        "You may add light framing or summary as long as it is marked "
        '"(my framing)".'
    )


def build_messages(
    *,
    question: str,
    top_rows: Sequence[CandidateRow],
    snippet_chars: int,
    history: Sequence[LLMMessage] | None = None,
) -> list[LLMMessage]:
    """Combine system + (optional) history + user prompts into one list.

    Order::

        [system, *history, user]

    ``history`` defaults to ``None`` (treated as empty). The
    non-conversational REST path passes nothing; the chat path passes
    the list returned by ``chat_history.fetch_recent_turns``.

    The returned list is what gets sent straight to the LLM provider.
    """
    user_msg = LLMMessage(
        role="user",
        content=build_user_prompt(
            question=question,
            top_rows=top_rows,
            snippet_chars=snippet_chars,
        ),
    )
    msgs: list[LLMMessage] = [LLMMessage(role="system", content=SYSTEM_PROMPT)]
    if history:
        msgs.extend(history)
    msgs.append(user_msg)
    return msgs


__all__ = [
    "SYSTEM_PROMPT",
    "build_messages",
    "build_user_prompt",
    "trim_snippet",
]
