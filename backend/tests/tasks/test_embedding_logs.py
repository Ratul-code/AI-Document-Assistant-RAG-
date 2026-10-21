"""Tests for the embedding worker's log + progress emissions.

The worker writes `document_logs` rows and pushes
`document.embedding_progress` events at well-defined checkpoints.
These tests pin the emission contract so a future refactor can't
silently drop them (the UI depends on them for the activity feed
and progress bars).

What's pinned:
  - `embedding.started` log row exists at the top, carrying
    `chunks_total` and `model`.
  - One `embedding.progress` log row per batch, with batch_index
    starting at 1.
  - One `embedding.progress` WS event per batch, with `chunks_done`
    accumulating across batches.
  - Terminal `ready` (or `failed`) log row matches the doc's final
    status.
  - A whole-batch failure still emits a `embedding.progress` log row
    so the activity feed shows what happened.
"""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine

from app.db.base import Base
from app.models.document import Document, DocumentStatus
from app.models.document_chunk import DocumentChunk
from app.models.document_log import DocumentLog, DocumentLogEventType
from app.models.document_page import DocumentPage, TextSource
from app.models.user import User
from app.services.pubsub import ws_publisher
from app.tasks.embedding import _process_one_document


@pytest.fixture
def sync_db_session(monkeypatch: pytest.MonkeyPatch):
    """In-memory SQLite session, with the embedding provider forced
    to the deterministic stub so tests don't pay the multi-second
    Qwen3 checkpoint load. The autouse `_reset_settings_cache`
    fixture in `tests/conftest.py` clears the settings lru_cache
    after `monkeypatch.setenv` so the next `get_settings()` reads
    the new value."""
    monkeypatch.setenv("EMBEDDING_PROVIDER", "stub")
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed(session, chunk_texts: list[str]):
    """Same shape as the existing `test_embedding_worker._seed`."""
    user = User(
        id=uuid4(),
        email=f"emb-log-{uuid4().hex[:8]}@example.com",
        name="Embedding Log Test",
        hashed_password="x" * 60,
    )
    session.add(user)
    doc = Document(
        id=uuid4(),
        owner_id=user.id,
        title="t",
        filename="f",
        mime_type="text/plain",
        file_size_bytes=10,
        storage_bucket="b",
        storage_path="p",
        status=DocumentStatus.learning,
    )
    session.add(doc)
    page = DocumentPage(
        id=uuid4(),
        document_id=doc.id,
        page_number=1,
        raw_text="hello",
        final_text="hello",
        text_source=TextSource.raw,
        char_count=5,
    )
    session.add(page)
    for idx, text in enumerate(chunk_texts):
        session.add(
            DocumentChunk(
                id=uuid4(),
                page_id=page.id,
                chunk_index=idx,
                text=text,
                token_count=2,
                char_count=len(text),
            )
        )
    session.commit()
    return user.id, doc.id


def _logs_for(session, doc_id) -> list[DocumentLog]:
    return list(
        session.execute(
            select(DocumentLog)
            .where(DocumentLog.document_id == doc_id)
            .order_by(DocumentLog.created_at.asc(), DocumentLog.id.asc())
        ).scalars()
    )


# ---------------------------------------------------------------------------
# Log-row emissions
# ---------------------------------------------------------------------------


def test_process_one_document_writes_started_log(sync_db_session) -> None:
    """The worker writes one `embedding.started` log row at the top,
    carrying `chunks_total` and the model name in `metadata`."""
    owner_id, doc_id = _seed(sync_db_session, ["a", "b", "c"])

    _process_one_document(sync_db_session, doc_id, owner_id)
    sync_db_session.commit()

    logs = _logs_for(sync_db_session, doc_id)
    started = [l for l in logs if l.event_type == DocumentLogEventType.embedding_started.value]
    assert len(started) == 1
    import json as _json
    meta = _json.loads(started[0].metadata_json)
    assert meta["chunks_total"] == 3
    assert "model" in meta
    # The model is whatever the test env configured — either the
    # deterministic stub or the real Qwen3 — but the field must be
    # present and a non-empty string.
    assert isinstance(meta["model"], str) and meta["model"]


def test_process_one_document_writes_one_progress_row_per_batch(
    sync_db_session,
) -> None:
    """6 chunks / batch_size=2 = 3 batches → 3 progress rows + 1 started + 1 ready."""
    owner_id, doc_id = _seed(
        sync_db_session, ["a", "b", "c", "d", "e", "f"]
    )

    _process_one_document(sync_db_session, doc_id, owner_id, batch_size=2)
    sync_db_session.commit()

    progress_rows = [
        l for l in _logs_for(sync_db_session, doc_id)
        if l.event_type == DocumentLogEventType.embedding_progress.value
    ]
    assert len(progress_rows) == 3

    # batch_index should be 1, 2, 3. We sort by the metadata's
    # batch_index rather than the row's created_at/id ordering,
    # because SQLite truncates created_at to second precision and
    # all three progress rows can be tied in the same transaction
    # (the test only commits at the end, so the auto-default
    # timestamp is the same for all three).
    import json as _json
    batch_indices = sorted(
        _json.loads(r.metadata_json)["batch_index"] for r in progress_rows
    )
    assert batch_indices == [1, 2, 3]

    # chunks_done accumulates: 2, 4, 6.
    chunks_done = [
        _json.loads(r.metadata_json)["chunks_done"]
        for r in sorted(
            progress_rows,
            key=lambda r: _json.loads(r.metadata_json)["batch_index"],
        )
    ]
    assert chunks_done == [2, 4, 6]


def test_process_one_document_writes_terminal_ready_log(sync_db_session) -> None:
    """A successful run ends with a `ready` log row carrying the
    succeeded/failed counts in metadata."""
    owner_id, doc_id = _seed(sync_db_session, ["a", "b"])

    _process_one_document(sync_db_session, doc_id, owner_id)
    sync_db_session.commit()

    ready_rows = [
        l for l in _logs_for(sync_db_session, doc_id)
        if l.event_type == DocumentLogEventType.ready.value
    ]
    assert len(ready_rows) == 1
    import json as _json
    meta = _json.loads(ready_rows[0].metadata_json)
    assert meta["succeeded"] == 2
    assert meta["failed"] == 0


def test_process_one_document_writes_terminal_failed_log_on_full_failure(
    sync_db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whole-batch failure → terminal `failed` log row, not `ready`."""

    class _AlwaysFailModel:
        model_name = "always-fail-test"
        dim = 16

        def embed_batch(self, texts: list[str]) -> list:
            raise RuntimeError("simulated provider outage")

        def embed(self, text: str):  # pragma: no cover
            raise RuntimeError("simulated provider outage")

    monkeypatch.setattr(
        "app.tasks.embedding.get_default_embedding_model", _AlwaysFailModel
    )

    owner_id, doc_id = _seed(sync_db_session, ["a", "b"])
    _process_one_document(sync_db_session, doc_id, owner_id)
    sync_db_session.commit()

    failed_rows = [
        l for l in _logs_for(sync_db_session, doc_id)
        if l.event_type == DocumentLogEventType.failed.value
    ]
    assert len(failed_rows) == 1


# ---------------------------------------------------------------------------
# Progress WS events
# ---------------------------------------------------------------------------


def test_process_one_document_publishes_progress_events(sync_db_session) -> None:
    """The worker pushes one `document.embedding_progress` WS event
    per batch (plus a started one). Pinned via a list capture so the
    test can assert the cumulative `chunks_done` field."""
    owner_id, doc_id = _seed(sync_db_session, ["a", "b", "c", "d"])

    captured: list[dict] = []

    def _capture(*args, **kwargs):
        captured.append(kwargs)
        return 0  # no subscribers

    # Patch the symbol the worker actually calls into. The helper
    # `_publish_progress` resolves `publish_embedding_progress` from
    # the module-level import in `app.tasks.embedding`, so patching
    # `ws_publisher.publish_embedding_progress` directly would not
    # intercept the call.
    with patch(
        "app.tasks.embedding.publish_embedding_progress", side_effect=_capture
    ):
        _process_one_document(sync_db_session, doc_id, owner_id, batch_size=2)
        sync_db_session.commit()

    # 1 started + 2 progress (4 chunks / batch_size=2).
    assert len(captured) == 3

    # First event is the started one: phase="embedding", chunks_total=4,
    # chunks_done=0.
    started = captured[0]
    assert started["phase"] == "embedding"
    assert started["chunks_total"] == 4
    assert started["chunks_done"] == 0
    assert started["model_name"]

    # Then one per batch, accumulating chunks_done.
    assert captured[1]["chunks_done"] == 2
    assert captured[2]["chunks_done"] == 4

    # `document_id` and `owner_id` are passed positionally as the
    # first arg; the rest are kwargs.
    assert all("phase" in c for c in captured)


def test_process_one_document_publishes_failed_event_on_full_failure(
    sync_db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whole-batch failure still pushes progress events (with the
    failed_total counter rising) and a terminal event."""

    class _AlwaysFailModel:
        model_name = "always-fail-test"
        dim = 16

        def embed_batch(self, texts: list[str]) -> list:
            raise RuntimeError("simulated provider outage")

        def embed(self, text: str):  # pragma: no cover
            raise RuntimeError("simulated provider outage")

    monkeypatch.setattr(
        "app.tasks.embedding.get_default_embedding_model", _AlwaysFailModel
    )

    owner_id, doc_id = _seed(sync_db_session, ["a", "b"])
    captured: list[dict] = []

    with patch(
        "app.tasks.embedding.publish_embedding_progress",
        side_effect=lambda *a, **k: captured.append(k) or 0,
    ):
        _process_one_document(sync_db_session, doc_id, owner_id)
        sync_db_session.commit()

    # 1 started + 1 progress (2 chunks in one batch). The progress
    # event's `failed_total` should be 2.
    progress_events = [c for c in captured if c.get("chunks_done") == 2]
    assert len(progress_events) == 1
    assert progress_events[0]["failed_total"] == 2


# ---------------------------------------------------------------------------
# No chunks → no progress, but the started log row is still emitted
# ---------------------------------------------------------------------------


def test_process_one_document_with_no_chunks_writes_only_started_log(
    sync_db_session,
) -> None:
    """Edge case: a doc with zero chunks should still get a started
    log row (so the activity feed shows we considered it) but no
    progress rows and no terminal ready/failed log."""
    owner_id, doc_id = _seed(sync_db_session, [])

    _process_one_document(sync_db_session, doc_id, owner_id)
    sync_db_session.commit()

    logs = _logs_for(sync_db_session, doc_id)
    event_types = [l.event_type for l in logs]
    assert event_types == [DocumentLogEventType.embedding_started.value]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
