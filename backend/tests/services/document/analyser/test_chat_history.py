"""Tests for the chat-history fetcher and the prompt builder's
``history=`` integration.

Covers:

* ``truncate_to_tokens`` — short / long / empty / zero-cap paths.
* ``fetch_recent_turns`` — order, error filtering, pair capping,
  assistant truncation, empty-conv short-circuit, defensive role
  normalisation (string vs enum vs SimpleNamespace).
* ``build_messages`` — ``history=[]`` is byte-shape-compatible with
  the v1 two-message prompt; non-empty ``history`` is inserted
  between system and user.

Mirrors the fake-store style in ``test_with_conversation.py`` so the
test surface area is consistent across the analyser tests.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

os.environ.setdefault("ANALYSIS_LLM_API_KEY", "sk-test")

from app.repositories.embedding_search import CandidateRow  # noqa: E402
from app.services.ai.llm import LLMMessage  # noqa: E402
from app.services.document.analyser import chat_history  # noqa: E402
from app.services.document.analyser.chat_history import (  # noqa: E402
    fetch_recent_turns,
    truncate_to_tokens,
)
from app.services.document.analyser.prompt import (  # noqa: E402
    SYSTEM_PROMPT,
    build_messages,
    build_user_prompt,
)

# ---------------------------------------------------------------------
# truncate_to_tokens
# ---------------------------------------------------------------------


def test_truncate_short_text_returned_unchanged():
    """Short text is returned as-is — no marker."""
    text = "short and sweet"
    out = truncate_to_tokens(text, max_tokens=400)
    assert out == text


def test_truncate_long_text_is_head_trimmed_with_marker():
    """Long text is trimmed from the head; the marker labels the cut."""
    long_text = ("lorem ipsum " * 200).strip()  # well over 400 tokens
    out = truncate_to_tokens(long_text, max_tokens=20)
    # The marker is present and the original HEAD is gone.
    assert out.startswith("…(roughly ")
    assert "earlier)" in out
    # The tail of the original text is preserved (we head-trim).
    tail_snippet = long_text[-40:]
    assert tail_snippet in out


def test_truncate_empty_and_none_return_empty():
    assert truncate_to_tokens("", max_tokens=100) == ""
    assert truncate_to_tokens(None, max_tokens=100) == ""  # type: ignore[arg-type]


def test_truncate_zero_or_negative_cap_returns_empty():
    text = "anything"
    assert truncate_to_tokens(text, max_tokens=0) == ""
    assert truncate_to_tokens(text, max_tokens=-5) == ""


# ---------------------------------------------------------------------
# fetch_recent_turns
# ---------------------------------------------------------------------


def _conv_messages(rows: list[tuple[str, str, str | None]]) -> list[SimpleNamespace]:
    """Build fake conversation-message rows.

    Each tuple is (role, content, error_code). ``error_code`` None
    means "successful turn".
    """
    out = []
    for _i, (role, content, err) in enumerate(rows):
        out.append(
            SimpleNamespace(
                id=uuid4(),
                conversation_id=uuid4(),
                role=role,  # tests verify string-tolerant normalisation
                content=content,
                error_code=err,
                created_at=None,
            )
        )
    return out


class _FakeConversationRepo:
    """In-memory shim for ``app.repositories.conversation.list_messages``.

    The real repo respects ``order``; tests pass rows in chronological
    order (oldest → newest) and the fake reverses when ``order="desc"``
    is requested. The fetcher under test always passes ``order="desc"``
    so it can slice the most-recent tail.
    """

    def __init__(self, rows: list[SimpleNamespace]) -> None:
        # Tests pass rows in chronological (oldest → newest) order.
        self._rows = rows

    async def list_messages(self, db, *, conversation_id, owner_id, limit, offset, order):
        out = list(self._rows)
        if order == "desc":
            out.reverse()
        return out[:limit]


def _patch_repo(monkeypatch, rows_desc):
    repo = _FakeConversationRepo(rows_desc)
    monkeypatch.setattr(chat_history, "conversation_repo", repo)
    return repo


@pytest.mark.asyncio
async def test_fetch_recent_turns_returns_pairs_oldest_first(monkeypatch):
    rows = _conv_messages(
        [
            ("user", "first q", None),
            ("assistant", "first a", None),
            ("user", "second q", None),
            ("assistant", "second a", None),
        ]
    )
    _patch_repo(monkeypatch, rows)

    msgs = await fetch_recent_turns(
        None,
        conversation_id=uuid4(),
        owner_id=uuid4(),
        max_turns=10,
        max_assistant_tokens=400,
    )

    assert [m.role for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert [m.content for m in msgs] == [
        "first q",
        "first a",
        "second q",
        "second a",
    ]


@pytest.mark.asyncio
async def test_fetch_recent_turns_drops_error_rows(monkeypatch):
    """An assistant error bubble is filtered; its preceding user
    row also drops (no successful pair)."""
    rows = _conv_messages(
        [
            ("user", "q1", None),
            ("assistant", "a1", None),
            ("user", "q2", None),
            ("assistant", "", "PROVIDER_ERROR"),
            # The most recent user row (q3) has no assistant yet —
            # it gets dropped as an orphan by the pair logic.
            ("user", "q3", None),
        ]
    )
    _patch_repo(monkeypatch, rows)

    msgs = await fetch_recent_turns(
        None,
        conversation_id=uuid4(),
        owner_id=uuid4(),
        max_turns=10,
        max_assistant_tokens=400,
    )
    assert [m.content for m in msgs] == ["q1", "a1"]


@pytest.mark.asyncio
async def test_fetch_recent_turns_caps_at_max_turns(monkeypatch):
    """max_turns=2 means at most 2 user/assistant pairs."""
    rows = _conv_messages(
        [
            ("user", "q1", None),
            ("assistant", "a1", None),
            ("user", "q2", None),
            ("assistant", "a2", None),
            ("user", "q3", None),
            ("assistant", "a3", None),
        ]
    )
    _patch_repo(monkeypatch, rows)

    msgs = await fetch_recent_turns(
        None,
        conversation_id=uuid4(),
        owner_id=uuid4(),
        max_turns=2,
        max_assistant_tokens=400,
    )
    # Most recent 2 pairs (q2/a2, q3/a3).
    assert [m.content for m in msgs] == ["q2", "a2", "q3", "a3"]


@pytest.mark.asyncio
async def test_fetch_recent_turns_truncates_long_assistant(monkeypatch):
    long_a = ("answer " * 200).strip()  # well over 400 tokens
    rows = _conv_messages(
        [
            ("user", "q1", None),
            ("assistant", long_a, None),
        ]
    )
    _patch_repo(monkeypatch, rows)

    msgs = await fetch_recent_turns(
        None,
        conversation_id=uuid4(),
        owner_id=uuid4(),
        max_turns=10,
        max_assistant_tokens=20,
    )
    assert len(msgs) == 2
    # User content is never truncated.
    assert msgs[0].content == "q1"
    # Assistant content is truncated with the marker.
    assert msgs[1].content.startswith("…(roughly ")


@pytest.mark.asyncio
async def test_fetch_recent_turns_short_circuits_on_zero(monkeypatch):
    """max_turns=0 returns [] without hitting the repo."""
    # No patch — if the repo is touched, AttributeError on the patch
    # is the failure signal.
    msgs = await fetch_recent_turns(
        None,
        conversation_id=uuid4(),
        owner_id=uuid4(),
        max_turns=0,
        max_assistant_tokens=400,
    )
    assert msgs == []


@pytest.mark.asyncio
async def test_fetch_recent_turns_handles_empty_repo(monkeypatch):
    _patch_repo(monkeypatch, [])
    msgs = await fetch_recent_turns(
        None,
        conversation_id=uuid4(),
        owner_id=uuid4(),
        max_turns=10,
        max_assistant_tokens=400,
    )
    assert msgs == []


@pytest.mark.asyncio
async def test_fetch_recent_turns_excludes_just_inserted_user(monkeypatch):
    """The user row we just inserted has no assistant yet — it must
    be excluded (orphan)."""
    rows = _conv_messages(
        [
            # Old exchange.
            ("user", "old q", None),
            ("assistant", "old a", None),
            # Just-inserted user row, no assistant yet.
            ("user", "new q", None),
        ]
    )
    _patch_repo(monkeypatch, rows)

    msgs = await fetch_recent_turns(
        None,
        conversation_id=uuid4(),
        owner_id=uuid4(),
        max_turns=10,
        max_assistant_tokens=400,
    )
    assert [m.content for m in msgs] == ["old q", "old a"]


# ---------------------------------------------------------------------
# build_messages — history integration
# ---------------------------------------------------------------------


def _candidate(text: str = "snippet text") -> CandidateRow:
    return CandidateRow(
        chunk_id=uuid4(),
        document_id=uuid4(),
        document_title="Doc",
        page_id=uuid4(),
        page_number=0,
        chunk_index=0,
        section_path=[],
        text=text,
        vector=[1.0, 0.0, 0.0, 0.0],
        embedding_model_name="fake-embed",
        embedding_dim=4,
    )


def test_build_messages_with_no_history_keeps_v1_shape():
    """The legacy non-conversational call returns [system, user] —
    the shape the REST endpoint already expects."""
    msgs = build_messages(
        question="What is X?",
        top_rows=[_candidate()],
        snippet_chars=200,
    )
    assert len(msgs) == 2
    assert msgs[0].role == "system"
    assert msgs[0].content == SYSTEM_PROMPT
    assert msgs[1].role == "user"
    assert "<CURRENT_QUESTION>" in msgs[1].content
    assert "What is X?" in msgs[1].content


def test_build_messages_with_history_inserts_between_system_and_user():
    history = [
        LLMMessage(role="user", content="earlier q"),
        LLMMessage(role="assistant", content="earlier a"),
    ]
    msgs = build_messages(
        question="follow-up",
        top_rows=[_candidate()],
        snippet_chars=200,
        history=history,
    )
    assert [m.role for m in msgs] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert msgs[0].content == SYSTEM_PROMPT
    assert msgs[1].content == "earlier q"
    assert msgs[2].content == "earlier a"
    assert "follow-up" in msgs[3].content


def test_build_messages_with_empty_history_matches_v1():
    """Empty list behaves identically to history=None."""
    msgs_none = build_messages(
        question="q",
        top_rows=[_candidate()],
        snippet_chars=200,
        history=None,
    )
    msgs_empty = build_messages(
        question="q",
        top_rows=[_candidate()],
        snippet_chars=200,
        history=[],
    )
    assert len(msgs_none) == len(msgs_empty) == 2
    assert msgs_none[0].content == msgs_empty[0].content
    assert msgs_none[1].content == msgs_empty[1].content


def test_build_user_prompt_groups_by_document():
    """Two snippets from the same doc share a DOC banner; snippets
    from a different doc get a second banner."""
    rows = [
        CandidateRow(
            chunk_id=uuid4(),
            document_id=uuid4(),
            document_title="Spec",
            page_id=uuid4(),
            page_number=2,
            chunk_index=0,
            section_path=[],
            text="Spec chunk one.",
            vector=[1.0, 0.0, 0.0, 0.0],
            embedding_model_name="fake-embed",
            embedding_dim=4,
        ),
        CandidateRow(
            chunk_id=uuid4(),
            document_id=uuid4(),
            document_title="Spec",
            page_id=uuid4(),
            page_number=4,
            chunk_index=1,
            section_path=[],
            text="Spec chunk two.",
            vector=[0.0, 1.0, 0.0, 0.0],
            embedding_model_name="fake-embed",
            embedding_dim=4,
        ),
        CandidateRow(
            chunk_id=uuid4(),
            document_id=uuid4(),
            document_title="Guide",
            page_id=uuid4(),
            page_number=0,
            chunk_index=0,
            section_path=[],
            text="Guide chunk.",
            vector=[0.0, 0.0, 1.0, 0.0],
            embedding_model_name="fake-embed",
            embedding_dim=4,
        ),
    ]
    prompt = build_user_prompt(question="q", top_rows=rows, snippet_chars=200)
    # Numbering is global: [1], [2], [3].
    assert "[1]" in prompt and "[2]" in prompt and "[3]" in prompt
    # Two DOC banners.
    assert prompt.count("DOC: ") == 2
    assert "DOC: Spec" in prompt
    assert "DOC: Guide" in prompt


__all__ = [
    "test_build_messages_with_empty_history_matches_v1",
    "test_build_messages_with_history_inserts_between_system_and_user",
    "test_build_messages_with_no_history_keeps_v1_shape",
    "test_build_user_prompt_groups_by_document",
    "test_fetch_recent_turns_caps_at_max_turns",
    "test_fetch_recent_turns_drops_error_rows",
    "test_fetch_recent_turns_excludes_just_inserted_user",
    "test_fetch_recent_turns_handles_empty_repo",
    "test_fetch_recent_turns_returns_pairs_oldest_first",
    "test_fetch_recent_turns_short_circuits_on_zero",
    "test_fetch_recent_turns_truncates_long_assistant",
    "test_truncate_empty_and_none_return_empty",
    "test_truncate_long_text_is_head_trimmed_with_marker",
    "test_truncate_short_text_returned_unchanged",
    "test_truncate_zero_or_negative_cap_returns_empty",
]
