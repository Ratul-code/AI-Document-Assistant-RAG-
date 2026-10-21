"""Tests for the dataset schema (`eval/schema.py`).

These pin three invariants:
1. Round-trip JSON: dump → load → equal.
2. `Literal` enforcement: invalid `status`, `scope_mode`, etc.
   raise `ValidationError`.
3. `extra="forbid"`: unknown fields raise.
4. UUID serialisation: `model_dump_json()` produces strings, and
   `model_validate_json()` parses them back.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from eval.schema import (
    Doc2DialDataset,
    MetricsConfig,
    PassageMapping,
    Question,
    RelevantChunkKey,
)


# Stable IDs so a failing test diff is easy to read.
OWNER = UUID("00000000-0000-0000-0000-000000000001")
DOC1 = UUID("00000000-0000-0000-0000-000000000010")
DOC2 = UUID("00000000-0000-0000-0000-000000000011")
CHUNK1 = UUID("00000000-0000-0000-0000-000000000020")
CHUNK2 = UUID("00000000-0000-0000-0000-000000000021")
CHUNK3 = UUID("00000000-0000-0000-0000-000000000022")


def _question(**overrides) -> Question:
    base = dict(
        id="doc2dial-0",
        question="What is the application process?",
        question_type="dialogue-turn",
        scope_document_ids=[DOC1, DOC2],
        relevant_chunk_keys=[
            RelevantChunkKey(document_id=DOC1, chunk_index=0),
            RelevantChunkKey(document_id=DOC2, chunk_index=1),
        ],
        relevant_passage_ids=["p0", "p1"],
        gold_answer="The application requires...",
    )
    base.update(overrides)
    return Question(**base)


def _dataset(**overrides) -> Doc2DialDataset:
    base = dict(
        name="doc2dial",
        version="v1",
        status="ready",
        scope_mode="per_question",
        passage_mapping="fuzzy_overlap_0.5",
        questions=[_question()],
        metrics_config=MetricsConfig(),
        created_at=datetime(2026, 10, 3, 10, 0, 0, tzinfo=timezone.utc),
        owner_id=OWNER,
        source_doc_count=6,
        question_type_counts={"dialogue-turn": 1},
    )
    base.update(overrides)
    return Doc2DialDataset(**base)


def test_dataset_roundtrip() -> None:
    ds = _dataset()
    serialised = ds.model_dump_json()
    reloaded = Doc2DialDataset.model_validate_json(serialised)
    assert reloaded == ds
    assert reloaded.questions[0].id == "doc2dial-0"
    assert reloaded.status == "ready"
    assert reloaded.metrics_config.top_k == 5


def test_invalid_status_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        _dataset(status="bogus")
    assert "status" in str(exc.value)


def test_invalid_scope_mode_rejected() -> None:
    with pytest.raises(ValidationError):
        _dataset(scope_mode="global_everything")


def test_invalid_passage_mapping_rejected() -> None:
    with pytest.raises(ValidationError):
        _dataset(passage_mapping="exact_match_1.0")


def test_extra_field_forbidden_on_dataset() -> None:
    with pytest.raises(ValidationError) as exc:
        _dataset(unknown_field=1)
    assert "unknown_field" in str(exc.value)


def test_extra_field_forbidden_on_question() -> None:
    with pytest.raises(ValidationError) as exc:
        _question(noise="oops")
    assert "noise" in str(exc.value)


def test_question_serialises_uuids_as_strings() -> None:
    q = _question()
    payload = q.model_dump(mode="json")
    # UUID fields are str in JSON mode.
    assert payload["scope_document_ids"] == [str(DOC1), str(DOC2)]
    assert payload["relevant_chunk_keys"] == [
        {"document_id": str(DOC1), "chunk_index": 0},
        {"document_id": str(DOC2), "chunk_index": 1},
    ]
    # Round-trip preserves the UUID type.
    reloaded = Question.model_validate(payload)
    assert reloaded.scope_document_ids == [DOC1, DOC2]
    assert len(reloaded.relevant_chunk_keys) == 2
    assert reloaded.relevant_chunk_keys[0].document_id == DOC1
    assert reloaded.relevant_chunk_keys[0].chunk_index == 0


def test_metrics_config_defaults() -> None:
    cfg = MetricsConfig()
    assert cfg.top_k == 5
    assert cfg.secondary_top_ks == [1, 10]
    assert cfg.min_cosine == 0.0


def test_metrics_config_top_k_bounds() -> None:
    with pytest.raises(ValidationError):
        MetricsConfig(top_k=0)
    with pytest.raises(ValidationError):
        MetricsConfig(top_k=51)
    # Boundary values are accepted.
    assert MetricsConfig(top_k=1).top_k == 1
    assert MetricsConfig(top_k=50).top_k == 50


def test_dataset_is_frozen() -> None:
    ds = _dataset()
    with pytest.raises(ValidationError):
        ds.status = "needs_manual_population"  # type: ignore[misc]


def test_passage_mapping_entry_bounds() -> None:
    # overlap_ratio must be 0..1.
    from eval.schema import PassageMappingEntry
    with pytest.raises(ValidationError):
        PassageMappingEntry(
            passage_id="p0",
            passage_text="x",
            chunk_id=CHUNK1,
            chunk_text="y",
            overlap_ratio=1.5,
        )
    with pytest.raises(ValidationError):
        PassageMappingEntry(
            passage_id="p0",
            passage_text="x",
            chunk_id=CHUNK1,
            chunk_text="y",
            overlap_ratio=-0.1,
        )


def test_passage_mapping_default_threshold() -> None:
    pm = PassageMapping(
        dataset_version="v1",
        heuristic="fuzzy_overlap_0.5",
    )
    assert pm.threshold == 0.5
    assert pm.entries == []