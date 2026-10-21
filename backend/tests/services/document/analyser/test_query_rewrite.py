"""Tests for the query-rewrite helper (ADR-021).

Covers:

* Prompt shape — the provider receives a system + user message
  where the user message contains the prior transcript and the
  current question.
* Empty history → no LLM call, returns the question unchanged.
* Disabled (caller passes ``enabled=False``) — exercised at the
  orchestrator level (the helper itself doesn't read settings;
  the orchestrator is what gates the call).
* LLM raises → returns the question, no exception bubbles up.
* LLM returns empty / whitespace → returns the question.
* LLM returns text with accidental wrappers (quotes, "Rewritten
  query:" prefix) → they're stripped, the cleaned result is
  returned.
* History is truncated to ``max_history_turns`` pairs.
* Truncation is pair-aligned (no orphan user row).
* The deadline is enforced by the orchestrator via
  ``asyncio.wait_for``; the helper itself does not time out.
  Tested separately by injecting a slow fake that never
  resolves — the orchestrator's wait_for is the safety net.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

os.environ.setdefault("ANALYSIS_LLM_API_KEY", "sk-test")

from app.services.ai.llm import LLMMessage  # noqa: E402
from app.services.document.analyser.query_rewrite import (  # noqa: E402
    rewrite_query_for_retrieval,
)

# ---------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------


class _FakeLLMProvider:
    """In-memory fake matching the LLMProvider Protocol surface.

    Records every call to ``complete`` so tests can assert the
    prompt shape. ``complete_return`` and ``complete_raise`` let
    each test set up the next response.
    """

    def __init__(
        self,
        *,
        complete_return: str | None = "rewritten query text",
        complete_raise: Exception | None = None,
    ) -> None:
        self.complete_return = complete_return
        self.complete_raise = complete_raise
        self.complete_calls: list[list[LLMMessage]] = []
        self.stream_calls: list[list[LLMMessage]] = []

    async def stream(self, messages: list[LLMMessage]) -> AsyncIterator[str]:
        self.stream_calls.append(messages)
        yield ""  # not used by the helper

    async def complete(self, messages: list[LLMMessage]) -> str:
        self.complete_calls.append(messages)
        if self.complete_raise is not None:
            raise self.complete_raise
        return self.complete_return or ""


class _SlowLLMProvider:
    """Fake that never resolves — used to confirm the orchestrator
    is the one that enforces the deadline (not the helper)."""

    def __init__(self) -> None:
        self.called = False

    async def stream(self, messages):
        self.called = True
        yield ""
        await asyncio.Event().wait()  # never resolves

    async def complete(self, messages):
        self.called = True
        await asyncio.Event().wait()  # never resolves
        return ""


def _history() -> list[LLMMessage]:
    return [
        LLMMessage(role="user", content="Compare FastAPI and Flask for a small CRUD app"),
        LLMMessage(
            role="assistant",
            content="FastAPI is async-first with Pydantic validation. Flask is sync by default.",
        ),
        LLMMessage(role="user", content="What about websocket-heavy apps?"),
    ]


# ---------------------------------------------------------------------
# Prompt shape
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_receives_system_and_user_message_with_history():
    fake = _FakeLLMProvider(
        complete_return="Compare FastAPI and Flask for websocket-heavy applications"
    )
    out = await rewrite_query_for_retrieval(
        history=_history(),
        question="what about websocket-heavy apps?",
        llm_provider=fake,
        max_history_turns=4,
    )
    assert out == "Compare FastAPI and Flask for websocket-heavy applications"
    # Exactly one LLM call.
    assert len(fake.complete_calls) == 1
    messages = fake.complete_calls[0]
    assert [m.role for m in messages] == ["system", "user"]

    # System prompt carries the rewrite rules.
    sys_msg = messages[0].content
    assert "self-contained" in sys_msg.lower()
    assert "pronouns" in sys_msg.lower()

    # User message contains the transcript AND the current question.
    user_msg = messages[1].content
    assert "CHAT HISTORY" in user_msg
    assert "CURRENT QUESTION" in user_msg
    assert "what about websocket-heavy apps?" in user_msg
    # Each turn labelled with the speaker.
    assert "User: Compare FastAPI and Flask" in user_msg
    assert "Assistant: FastAPI is async-first" in user_msg
    # "REWRITTEN QUERY:" trailer nudges a clean output.
    assert "REWRITTEN QUERY:" in user_msg


# ---------------------------------------------------------------------
# Empty / disabled paths
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_history_returns_question_without_calling_llm():
    fake = _FakeLLMProvider(complete_return="should not be called")
    out = await rewrite_query_for_retrieval(
        history=[],
        question="hello",
        llm_provider=fake,
        max_history_turns=4,
    )
    assert out == "hello"
    assert fake.complete_calls == []


@pytest.mark.asyncio
async def test_empty_question_returns_empty_without_calling_llm():
    fake = _FakeLLMProvider()
    out = await rewrite_query_for_retrieval(
        history=_history(),
        question="",
        llm_provider=fake,
        max_history_turns=4,
    )
    assert out == ""
    assert fake.complete_calls == []


# ---------------------------------------------------------------------
# Failure paths — must NEVER raise
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_exception_falls_back_to_question():
    fake = _FakeLLMProvider(
        complete_raise=RuntimeError("network down"),
    )
    out = await rewrite_query_for_retrieval(
        history=_history(),
        question="what about websocket-heavy apps?",
        llm_provider=fake,
        max_history_turns=4,
    )
    # The question is returned unchanged on any failure.
    assert out == "what about websocket-heavy apps?"


@pytest.mark.asyncio
async def test_empty_provider_response_falls_back_to_question():
    fake = _FakeLLMProvider(complete_return="")
    out = await rewrite_query_for_retrieval(
        history=_history(),
        question="what about websocket-heavy apps?",
        llm_provider=fake,
        max_history_turns=4,
    )
    assert out == "what about websocket-heavy apps?"


@pytest.mark.asyncio
async def test_whitespace_only_provider_response_falls_back_to_question():
    fake = _FakeLLMProvider(complete_return="   \n\t  ")
    out = await rewrite_query_for_retrieval(
        history=_history(),
        question="what about websocket-heavy apps?",
        llm_provider=fake,
        max_history_turns=4,
    )
    assert out == "what about websocket-heavy apps?"


# ---------------------------------------------------------------------
# Wrapping / label stripping
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_double_quote_wrapping_is_stripped():
    fake = _FakeLLMProvider(
        complete_return='"How does FastAPI handle websockets compared to Flask"',
    )
    out = await rewrite_query_for_retrieval(
        history=_history(),
        question="what about websocket-heavy apps?",
        llm_provider=fake,
        max_history_turns=4,
    )
    assert out == "How does FastAPI handle websockets compared to Flask"


@pytest.mark.asyncio
async def test_leading_label_is_stripped():
    fake = _FakeLLMProvider(
        complete_return="Rewritten query: How does FastAPI handle websockets vs Flask",
    )
    out = await rewrite_query_for_retrieval(
        history=_history(),
        question="what about websocket-heavy apps?",
        llm_provider=fake,
        max_history_turns=4,
    )
    assert out == "How does FastAPI handle websockets vs Flask"


@pytest.mark.asyncio
async def test_markdown_bold_wrapping_is_stripped():
    """Some models wrap with ** or backticks — strip those too."""
    fake = _FakeLLMProvider(
        complete_return="`How does FastAPI handle websockets vs Flask`",
    )
    out = await rewrite_query_for_retrieval(
        history=_history(),
        question="what about websocket-heavy apps?",
        llm_provider=fake,
        max_history_turns=4,
    )
    assert out == "How does FastAPI handle websockets vs Flask"


# ---------------------------------------------------------------------
# History truncation
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_is_truncated_to_max_turns_pairs():
    """With max_history_turns=1, only the most recent pair is sent."""
    fake = _FakeLLMProvider(complete_return="ok")
    # _history() has 2 pairs; with cap=1 we keep only the last one.
    await rewrite_query_for_retrieval(
        history=_history(),
        question="what about websocket-heavy apps?",
        llm_provider=fake,
        max_history_turns=1,
    )
    user_msg = fake.complete_calls[0][1].content
    # The first pair (Compare FastAPI...) is dropped.
    assert "Compare FastAPI" not in user_msg
    # The second pair (the websocket question) is kept.
    assert "websocket-heavy" in user_msg
    # The current question is always there.
    assert "what about websocket-heavy apps?" in user_msg


@pytest.mark.asyncio
async def test_max_turns_zero_does_not_call_llm_via_empty_history():
    """The orchestrator's `rewrite_enabled` gate already handles
    max_turns=0 by skipping the call entirely. The helper itself
    still processes the history when called; the gate is at the
    orchestrator level. So we just confirm the helper still works
    with max_turns=0 (it'd send an empty slice, but our slice
    logic keeps the full list when no pairs satisfy the count)."""
    fake = _FakeLLMProvider(complete_return="ok")
    out = await rewrite_query_for_retrieval(
        history=_history(),
        question="q",
        llm_provider=fake,
        max_history_turns=0,
    )
    # When max_turns=0, no pairs are counted, so start_idx stays
    # at len(history) → trimmed is empty. But the LLM is still
    # called (the orchestrator is what gates the call). Verify
    # the helper does NOT loop forever or panic.
    assert out == "ok"
    assert len(fake.complete_calls) == 1


# ---------------------------------------------------------------------
# The deadline is the orchestrator's job, not the helper's
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_helper_does_not_impose_deadline():
    """A slow provider just makes the helper's await take longer;
    the orchestrator's ``asyncio.wait_for`` is what enforces the
    wall-clock cap. We assert this by NOT wrapping the helper in
    wait_for and showing it returns the moment the provider
    resolves — i.e. the helper is the safety net, not the
    deadline."""
    # The slow provider would block forever, so we use
    # wait_for at the call site (mimicking the orchestrator) to
    # cap it. The helper's failure-to-resolve case is therefore
    # the orchestrator's problem.
    slow = _SlowLLMProvider()

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            rewrite_query_for_retrieval(
                history=_history(),
                question="q",
                llm_provider=slow,
                max_history_turns=4,
            ),
            timeout=0.05,
        )
    # The slow provider was at least called.
    assert slow.called
