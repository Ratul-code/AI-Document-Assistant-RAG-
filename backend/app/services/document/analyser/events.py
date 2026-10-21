"""In-process analysis events.

The orchestrator (`analyse_query`) yields `AnalysisEvent`s to its
caller — these are the **rich**, internal types. They are translated
into wire-format `WsEvent` envelopes at the boundary (REST handler
or the background streaming task). Keeping them separate means the
service can yield richer data (e.g. an `intermediate_citations`
event) without changing the wire protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

AnalysisEventKind = Literal["token", "done", "error"]


@dataclass(frozen=True)
class AnalysisTokenEvent:
    """One token delta from the LLM.

    `conversation_id` is set when the orchestrator is running under a
    persistent conversation — the WS publisher threads it through to
    the envelope so the chat UI can reconcile the stream with the
    open chat. ``None`` for the legacy one-shot query path.
    """

    kind: Literal["token"] = "token"
    text: str = ""
    conversation_id: UUID | None = None


@dataclass(frozen=True)
class AnalysisDoneEvent:
    """The terminal, successful event for a stream.

    `answer` is the concatenated text of every token yielded before
    this event. `citations` mirrors `QueryResponse.citations` so the
    REST fallback and the WS payload are byte-identical. Stored as
    dicts so they survive the WS publisher round-trip without a
    pydantic re-validation; the REST fallback re-hydrates them via
    `QueryCitation.model_validate(...)`.
    """

    kind: Literal["done"] = "done"
    answer: str = ""
    citations: list[dict[str, Any]] | None = None
    conversation_id: UUID | None = None


@dataclass(frozen=True)
class AnalysisErrorEvent:
    """Terminal failure for a stream.

    `code` matches `AnalysisErrorCode` so the caller (REST handler or
    WS publisher) maps it to the same wire payload.
    """

    kind: Literal["error"] = "error"
    code: str = "INTERNAL"
    message: str = ""
    conversation_id: UUID | None = None


AnalysisEvent = AnalysisTokenEvent | AnalysisDoneEvent | AnalysisErrorEvent


# Public re-exports — convenient for callers importing from the package.
__all__ = [
    "AnalysisDoneEvent",
    "AnalysisErrorEvent",
    "AnalysisEvent",
    "AnalysisTokenEvent",
]
