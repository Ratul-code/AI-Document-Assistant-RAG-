"""Chat history support for the RAG (analysis) pipeline.

The orchestrator persists every turn to ``conversation_messages`` but
the prompt-building path never read them back — so follow-up questions
("the second one", "as I asked above", "extend that table") had no
context. This module closes that loop by fetching the most recent
non-error turns and turning them into ``LLMMessage`` rows that slot
between the system prompt and the current user question.

Design notes
------------

* **Pure module.** No I/O beyond the single repo call. No LLM call.
  Tests can drive it with a fake ``list_messages`` shim.

* **One new public surface.** ``fetch_recent_turns`` returns a
  ``list[LLMMessage]`` ordered oldest → newest. The caller in
  ``analyse_query_with_conversation`` passes the list straight into
  ``build_messages(history=...)`` — no post-processing.

* **Hard token cap, soft truncation.** We hard-trim long assistant
  content with a leading marker so the model knows the response was
  cut. User messages are never trimmed (they're already ≤ 2000 chars
  by schema, and a half-user-question in the middle of the prompt
  would be worse than a few extra tokens).

* **Errors are filtered out.** A red error bubble ("PROVIDER_ERROR",
  "NO_DOCUMENTS_READY") is a UI affordance, not signal. Including it
  in the prompt would prime the next answer with the failure mode.

* **No summarisation yet.** The plan deliberately ships a sliding
  window only. A rolling-summary stage is a follow-up; the 1M-token
  context buys us plenty of headroom before it becomes necessary.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence
from uuid import UUID

from app.repositories import conversation as conversation_repo
from app.services.ai.llm import LLMMessage

logger = logging.getLogger(__name__)


# A conservative 4-chars-per-token ratio for the marker overhead
# estimate. The real count uses tiktoken below; this is only for the
# "N tokens earlier" label so the label is roughly right.
_CHARS_PER_TOKEN = 4

# Lazy tiktoken import. The chunker already uses ``cl100k_base``;
# keep the encoder alive in module state so the first call doesn't
# pay the load cost on every request.
_encoder = None


def _get_encoder():
    global _encoder
    if _encoder is None:
        import tiktoken

        _encoder = tiktoken.get_encoding("cl100k_base")
    return _encoder


def count_tokens(text: str) -> int:
    """Return the cl100k_base token count for ``text``.

    Empty / None input → 0. Used by the truncation helper to decide
    when a head-trim is required and to label the omitted chunk in
    the marker.
    """
    if not text:
        return 0
    return len(_get_encoder().encode(text, disallowed_special=()))


def truncate_to_tokens(text: str, *, max_tokens: int) -> str:
    """Hard-trim ``text`` to ≤ ``max_tokens`` tokens with a leading marker.

    Short text (≤ max_tokens) is returned unchanged. Long text is
    head-trimmed: the model sees the closing argument and a marker
    like ``"…(roughly 312 tokens earlier)"`` so it knows the
    response was cut. We prefer keeping the tail because the
    question's answer usually lands in the closing paragraphs.

    Truncation is exact at the token boundary, then the surrounding
    text is decoded back to a string for the marker label (which is
    a char-count estimate; the model doesn't parse it).

    Edge cases:
        * ``text`` empty / None → returns ``""``.
        * ``max_tokens <= 0`` → returns ``""`` (caller's intent is
          "drop the assistant turn" — slightly aggressive but safe).
        * ``text`` *shorter* than the budget → returned unchanged.
    """
    if not text:
        return ""
    if max_tokens <= 0:
        return ""
    if count_tokens(text) <= max_tokens:
        return text

    # Encode, drop the head, decode. Decoding a token slice can yield
    # a couple of replacement chars at the boundary; that's fine —
    # we're already labelling the cut.
    enc = _get_encoder()
    ids = enc.encode(text, disallowed_special=())
    total = len(ids)
    kept = ids[-max_tokens:]
    kept_text = enc.decode(kept)
    omitted = max(0, total - max_tokens)
    # Char-count approximation in the label. Lying slightly in the
    # label is fine; the model's job is to treat prior content as
    # evidence, and a labelled cut is still better than a silent cut.
    approx_chars = omitted * _CHARS_PER_TOKEN
    return f"…(roughly {omitted} tokens / ~{approx_chars} chars earlier)\n{kept_text}"


async def fetch_recent_turns(
    db: Any,
    *,
    conversation_id: UUID,
    owner_id: UUID,
    max_turns: int,
    max_assistant_tokens: int,
) -> list[LLMMessage]:
    """Return the most recent non-error turns for ``conversation_id``.

    Args:
        db: AsyncSession. The caller owns the transaction; this
            function only reads.
        conversation_id, owner_id: standard owner-scoped filters.
        max_turns: Maximum NUMBER OF PAIRS. The repo's
            ``list_messages`` returns rows ordered oldest → newest;
            we slice off the tail. ``0`` returns ``[]`` without
            hitting the DB.
        max_assistant_tokens: Per-assistant-message token cap passed
            to :func:`truncate_to_tokens`.

    Returns:
        A ``list[LLMMessage]`` ordered oldest → newest, alternating
        ``user`` / ``assistant``. Empty list on no rows or a
        misconfigured ``max_turns``. Never raises for "no rows" or
        "no conversation" — the caller already checked ownership
        upstream and the prompt just degrades to chat-less.

    Defensive construction: the repo returns either a real
    SQLAlchemy row (with ``role`` as a ``MessageRole`` enum, possibly
    with a ``.value`` attribute) or a unit-test fake
    (``SimpleNamespace`` with ``role`` already a string). Both
    paths are handled below so tests don't need a real session.
    """
    if max_turns <= 0:
        return []

    # We need at most 2 * max_turns rows (one user + one assistant
    # per pair). Pull from the most recent end with `order="desc"`
    # + limit; reverse the slice into oldest → newest so the model
    # reads it in natural order.
    limit = max_turns * 2
    rows = await conversation_repo.list_messages(
        db,
        conversation_id=conversation_id,
        owner_id=owner_id,
        limit=limit,
        offset=0,
        order="desc",
    )
    if not rows:
        return []

    # Filter out error rows. ``error_code`` is a column on the
    # assistant side only; user rows have it as None. Use getattr so
    # test fakes that omit the attribute still work.
    def _is_error(row: Any) -> bool:
        return bool(getattr(row, "error_code", None))

    filtered = [r for r in rows if not _is_error(r)]

    # We over-fetched in case errors filled the slice. Re-check the
    # pair count after filtering.
    filtered = filtered[:limit]
    # Order is currently newest → oldest (we asked `desc`); reverse.
    filtered.reverse()

    # Now apply the pair cap. Walk oldest → newest counting
    # consecutive user/assistant pairs so we don't ship an
    # unbalanced slice (e.g. a user row whose assistant row got
    # truncated by the pair cap).
    out: list[LLMMessage] = []
    pending_user: tuple[Any, str] | None = None  # (row, content) waiting on assistant
    pairs = 0
    for row in filtered:
        role = _role_str(row)
        content = getattr(row, "content", "") or ""
        if role == "user":
            # If a previous user row never got an assistant, we
            # don't ship it alone — drop the orphan.
            pending_user = (row, content)
        elif role == "assistant":
            user_payload = pending_user
            pending_user = None
            if user_payload is None:
                # Assistant without a preceding user in the slice
                # (shouldn't happen because rows are user/assistant
                # pairs, but be defensive). Skip the orphan.
                continue
            out.append(LLMMessage(role="user", content=user_payload[1]))
            truncated = truncate_to_tokens(
                content, max_tokens=max_assistant_tokens
            )
            out.append(LLMMessage(role="assistant", content=truncated))
            pairs += 1
            if pairs >= max_turns:
                break
        else:
            # Unknown role — skip.
            continue

    if out:
        logger.info(
            "fetch_recent_turns: conv_id=%s pairs=%d total_msgs=%d",
            conversation_id, pairs, len(out),
        )
    return out


def _role_str(row: Any) -> str:
    """Normalise ``row.role`` to ``"user"`` / ``"assistant"``.

    Real SQLAlchemy rows carry a ``MessageRole`` enum; tests pass
    either a string or a SimpleNamespace with ``.value``. We accept
    all three without raising.
    """
    role = getattr(row, "role", None)
    if role is None:
        return ""
    if hasattr(role, "value"):
        return str(role.value)
    return str(role)


__all__ = [
    "count_tokens",
    "fetch_recent_turns",
    "truncate_to_tokens",
]
