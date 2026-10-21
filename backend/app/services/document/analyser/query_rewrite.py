"""Conversational query rewriting for retrieval (ADR-021).

The retriever embeds the user's current question and (in hybrid mode)
tokenizes it for BM25. On a follow-up turn ("what about the second
one you mentioned?", "extend that table"), the raw question is a bag
of pronouns that match nothing in the document corpus — top-K drifts
into noise, the reranker has nothing to promote, and the answer-
generation LLM gets the wrong neighbourhood.

This module closes that loop with a cheap rewrite step:

  1. Take the last ``max_history_turns`` pairs (oldest → newest) +
     the current question.
  2. Send them to a small LLM (Flash-Lite by default) with a short
     system prompt that asks for a single self-contained sentence.
  3. Use the rewritten sentence for embedding and BM25 — and also
     feed it to the cross-encoder reranker, since (query, doc) is
     exactly the right input shape for a cross-encoder.

The answer-generation prompt is NOT changed: it still gets the raw
chat history and the raw question so it can answer naturally with
pronouns. The rewrite is *only* for the retriever.

Fail-soft contract
-------------------

This module NEVER raises for retrieval-side failures:

  * empty history → returns the raw question, no LLM call
  * disabled via env → returns the raw question, no LLM call
  * LLM timeout → returns the raw question, logs a warning
  * LLM exception → returns the raw question, logs a warning
  * LLM returns empty / whitespace → returns the raw question

The upstream orchestrator is the only place the deadline is enforced
(``asyncio.wait_for``); this helper is the safety net inside it.
That separation lets us unit-test the fallback paths without
mocking event loops, and lets the orchestrator tune the deadline
without touching this module.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from app.services.ai.llm import LLMMessage, LLMProvider

logger = logging.getLogger(__name__)


# System prompt kept deliberately short — Flash-Lite responds well
# to structured instructions and over-instructing makes it cautious.
_REWRITE_SYSTEM_PROMPT = """\
You rewrite a follow-up question into a self-contained query that \
can be embedded against a document corpus. The rewritten query \
must NOT require the chat history to understand.

Rules:
- Resolve all pronouns ("it", "the second one", "that table", \
"the one you mentioned") against the chat history.
- Replace vague references with the actual noun from the history \
("FastAPI", not "the async one").
- Keep the user's intent — do not add constraints they did not \
express.
- Output ONE line. No preamble, no quotes, no bullet points, no \
markdown.
"""


def _format_history(history: Sequence[LLMMessage]) -> str:
    """Render prior turns as a transcript, oldest → newest.

    The ``User:`` / ``Assistant:`` prefixes are critical — without
    them the model can't reliably tell who said what, and our
    pronoun-resolution prompt relies on that distinction.
    """
    lines: list[str] = []
    for msg in history:
        role = "User" if msg.role == "user" else "Assistant"
        lines.append(f"{role}: {msg.content}")
    return "\n".join(lines)


def _build_messages(
    *,
    history: Sequence[LLMMessage],
    question: str,
) -> list[LLMMessage]:
    """Build the [system, user] message pair for one rewrite call."""
    user_body = (
        "CHAT HISTORY (most recent last):\n"
        f"{_format_history(history)}\n\n"
        "CURRENT QUESTION:\n"
        f"{question}\n\n"
        "REWRITTEN QUERY:"
    )
    return [
        LLMMessage(role="system", content=_REWRITE_SYSTEM_PROMPT),
        LLMMessage(role="user", content=user_body),
    ]


def _clean(text: str) -> str:
    """Strip accidental wrappers the rewrite model sometimes emits.

    The system prompt says "no preamble, no quotes, no markdown" but
    small models occasionally violate that. We tolerate common
    accidents:

      * surrounding quotes (single, double, backtick)
      * leading "Rewritten query:" / "Here's the rewrite:" / etc.
      * trailing whitespace

    We do NOT strip aggressively — the rewrite is going to be
    embedded, and a slightly verbose but coherent rewrite beats a
    partial one. We only remove obvious wrapping.
    """
    s = (text or "").strip()
    if not s:
        return s
    # Strip a single wrapping quote pair, e.g. `"how does..."` or `'x'`.
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'", "`"):
        s = s[1:-1].strip()
    # Strip leading labels the model sometimes echoes.
    lower = s.lower()
    for prefix in (
        "rewritten query:",
        "rewrite:",
        "here's the rewrite:",
        "here is the rewrite:",
        "the rewritten query:",
    ):
        if lower.startswith(prefix):
            s = s[len(prefix) :].lstrip()
            break
    return s.strip()


async def rewrite_query_for_retrieval(
    *,
    history: Sequence[LLMMessage],
    question: str,
    llm_provider: LLMProvider,
    max_history_turns: int,
) -> str:
    """Return a self-contained rewritten query; never raises.

    Args:
        history: Prior chat turns (oldest → newest). Pass an empty
            list when there's no conversation context — the helper
            short-circuits and returns ``question``.
        question: The user's current question text.
        llm_provider: An ``LLMProvider`` instance. The caller (the
            orchestrator) builds this via ``get_rewrite_llm_provider``
            in production and injects a stub in tests.
        max_history_turns: Maximum number of PAIRS (user+assistant)
            to include. Older turns are dropped; the most recent
            N are kept. The helper slices the tail internally.

    Returns:
        A non-empty string. On any failure path, returns
        ``question`` unchanged so retrieval is never worse than v1.

    Notes:
        * The caller is expected to wrap this in ``asyncio.wait_for``
          to enforce a wall-clock deadline — the helper itself does
          not impose one, so a hung provider is visible at the
          ``analyse_query`` level where it can be timed out cleanly.
        * ``LLMProvider.complete`` is used (non-streaming) because
          we want the whole rewritten string before embedding; the
          answer-gen path streams.
    """
    if not question:
        return ""
    if not history:
        # No conversational context — nothing to resolve against.
        # Save the LLM call.
        return question

    # Slice the tail to the last max_history_turns pairs. Walk
    # from the end counting pairs so we don't ship an unbalanced
    # slice (e.g. a user row whose assistant row got truncated by
    # the cap). Mirrors the logic in chat_history.fetch_recent_turns
    # but applied to an in-memory list (cheap).
    pairs = 0
    start_idx = len(history)
    for i in range(len(history) - 1, -1, -1):
        msg = history[i]
        # Count each pair when we walk past its USER row (oldest
        # side of the pair).
        if msg.role == "user":
            pairs += 1
            if pairs >= max_history_turns:
                start_idx = i
                break
    else:
        # Didn't break — fewer pairs than max_history_turns.
        start_idx = 0
    trimmed = history[start_idx:]

    messages = _build_messages(history=trimmed, question=question)
    try:
        raw = await llm_provider.complete(messages)
    except Exception as exc:  # noqa: BLE001 — defensive: rewrite never breaks retrieval
        logger.warning(
            "query_rewrite: provider raised %s; falling back to raw question.",
            exc,
        )
        return question

    cleaned = _clean(raw)
    if not cleaned:
        logger.warning(
            "query_rewrite: provider returned empty after cleaning; "
            "falling back to raw question.",
        )
        return question
    return cleaned


__all__ = ["rewrite_query_for_retrieval"]
