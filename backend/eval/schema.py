"""Pydantic v2 models for the eval dataset + audit artefacts.

Three responsibilities:

1. `Doc2DialDataset` — the canonical, committable dataset file
   (`dataset.json`) consumed by the runner. Frozen + `extra=forbid`
   so a typo in an editor breaks loudly.
2. `PassageMapping` + `PassageMappingEntry` — the audit file
   (`passage_mapping.json`) that records every passage→chunk
   assignment with its overlap ratio. The runner never reads this;
   it's for reviewers who want to audit why a chunk was or wasn't
   marked relevant.
3. `MetricsConfig` — the per-dataset scoring configuration (top-K
   + cosine threshold). Embedded inside `Doc2DialDataset` so a
   runner invocation that overrides `--top-k` is explicit and
   recorded.

Gold chunks are referenced by `(document_id, chunk_index)` tuples
(see `RelevantChunkKey`), NOT by `chunk_id` UUID. The chunker
assigns random UUIDs on every ingestion (`uuid4()` in
`app/services/document/chunker/build_chunks.py`), so storing chunk
UUIDs would couple the dataset to one specific run. `(doc_id,
chunk_index)` is content-deterministic and survives re-seeds.

Every model is `frozen=True` and `extra="forbid"`. `Literal` enums
make invalid values raise at load time, not at run time.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# --------------------------------------------------------------------------
# Dataset (canonical, committable, read by the runner)
# --------------------------------------------------------------------------


class RelevantChunkKey(BaseModel):
    """Stable (document_id, chunk_index) tuple for one gold chunk.

    The chunker assigns random `chunk_id` UUIDs on every ingestion
    (`uuid4()` in `app/services/document/chunker/build_chunks.py`).
    Storing chunk UUIDs in the dataset would couple the dataset to
    one specific ingestion — useless when the hermetic DB is rebuilt
    every CLI run.

    The fix: store `(document_id, chunk_index)` instead. Document
    UUIDs are deterministic via UUID5, and chunk_index within a
    document is deterministic given the same input text.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: UUID
    chunk_index: int = Field(..., ge=0)


class Question(BaseModel):
    """One question with its scope, gold relevance, and gold answer.

    `scope_document_ids` mirrors Doc2Dial's per-question "documents"
    field, resolved to our `Document.id` UUIDs by `build_dataset.py`.
    The runner passes them to `fetch_candidate_rows(..., document_ids=...)`
    so the candidate set matches the production "selected doc scope"
    path (Option A from the design discussion).

    `relevant_chunk_keys` is the *canonical* gold set for scoring,
    derived from the passage→chunk mapping. `relevant_passage_ids` is
    Doc2Dial's raw passage ids, kept for audit. The two lists are not
    the same length: a single passage may map to one chunk, two
    chunks (boundary case), or zero chunks (false negative).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(..., description="Doc2Dial question id (stable across runs)")
    question: str = Field(..., description="The user's natural-language question")
    question_type: str = Field(
        ...,
        description="Doc2Dial's question-type label (free-form: dialogue-turn, etc.)",
    )
    scope_document_ids: list[UUID] = Field(
        default_factory=list,
        description="Per-question document scope (Option A from the design)",
    )
    relevant_chunk_keys: list[RelevantChunkKey] = Field(
        default_factory=list,
        description=(
            "Canonical gold (doc_id, chunk_index) pairs, derived from the "
            "passage→chunk mapping. Stable across re-seed invocations "
            "because chunk_index is content-deterministic."
        ),
    )
    relevant_passage_ids: list[str] = Field(
        default_factory=list,
        description="Doc2Dial's raw passage ids (audit only, not used by the runner)",
    )
    gold_answer: str = Field(
        default="",
        description="Doc2Dial's `response` field (audit only; v1 does not score answers)",
    )


class MetricsConfig(BaseModel):
    """Per-dataset scoring configuration.

    `top_k` is the primary recall target. `secondary_top_ks` are
    reported alongside it (typically `[1, 10]`).
    `min_cosine` matches the production analyser setting and is
    passed through to `rank_top_rows`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    top_k: int = Field(default=5, ge=1, le=50)
    secondary_top_ks: list[int] = Field(default_factory=lambda: [1, 10])
    min_cosine: float = Field(default=0.0, ge=-1.0, le=1.0)


class Doc2DialDataset(BaseModel):
    """The versioned, committable dataset file (`dataset.json`).

    The runner refuses to start if `status != "ready"` — `needs_manual_population`
    means the file is a placeholder and the numbers in it are not
    trustworthy. The status flag is the harness's quality gate
    (ADR-015: "No fake results").

    `name` is fixed to `doc2dial` — the runner is dataset-scoped to
    Doc2Dial; the `name` is just the dataset tag written into
    `dataset.json` and surfaced in the report filename.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Literal["doc2dial"]
    version: str = Field(..., description="Semantic version, e.g. 'v1'")
    status: Literal["needs_manual_population", "ready"]
    scope_mode: Literal["per_question", "owner_wide"]
    passage_mapping: Literal["fuzzy_overlap_0.5"]
    questions: list[Question]
    metrics_config: MetricsConfig = Field(default_factory=MetricsConfig)
    created_at: datetime
    owner_id: UUID = Field(
        ...,
        description="The synthetic eval owner; persisted to owner_id.txt and recorded here for cross-run stability",
    )
    source_doc_count: int = Field(..., ge=0)
    question_type_counts: dict[str, int] = Field(
        default_factory=dict,
        description="Histogram of question_type labels (for the report's stratification table)",
    )


# --------------------------------------------------------------------------
# Audit-only mapping file (not used by the runner)
# --------------------------------------------------------------------------


class PassageMappingEntry(BaseModel):
    """One passage→chunk match for audit.

    `overlap_ratio` is `lcs_ratio(passage, chunk)`. When the same
    passage maps to two chunks (boundary case), two entries are
    emitted. When no chunk's ratio crosses the threshold, no entry
    is emitted and `Question.relevant_chunk_keys` is empty — a known
    false negative of the heuristic.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    passage_id: str
    passage_text: str
    document_id: UUID
    chunk_index: int = Field(..., ge=0)
    chunk_text: str
    overlap_ratio: float = Field(..., ge=0.0, le=1.0)


class PassageMapping(BaseModel):
    """The audit file: every passage→chunk assignment with its ratio.

    One per (question, document) pair. The runner does not read this;
    `build_dataset.py` writes it and reviewers use it to audit
    `dataset.json`'s `relevant_chunk_keys`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_version: str
    heuristic: Literal["fuzzy_overlap_0.5"]
    threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    entries: list[PassageMappingEntry] = Field(default_factory=list)


__all__ = [
    "Doc2DialDataset",
    "MetricsConfig",
    "PassageMapping",
    "PassageMappingEntry",
    "Question",
    "RelevantChunkKey",
]
