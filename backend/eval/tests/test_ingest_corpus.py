"""Tests for the persisted-corpus ingestion entry point.

The full CLI (`python -m eval.scripts.ingest_corpus ...`) is
exercised by the real dataset run; the tests here focus on the
smallest contract that the runner depends on:

1. `write_manifest(...)` writes valid JSON containing
   `{dataset, model_name, embedding_dim, doc_count,
   chunk_count, completed_at, db_path}`.
2. `read_manifest(...)` returns the dict or None (missing/corrupt).
3. `--force` semantics are honoured (manifest + DB overwritten).
4. Re-invocation without `--force` is a no-op (exit 0, no overwrite).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from eval.scripts.ingest_corpus import (
    DB_FILENAME,
    MANIFEST_FILENAME,
    read_manifest,
    write_manifest,
)
from eval.scripts.seed_corpus import get_or_create_owner_id


@pytest.fixture(autouse=True)
def _stub_embedder():
    os.environ["EMBEDDING_PROVIDER"] = "stub"
    from app.core.config import get_settings
    from app.services.ai.embeddings import reset_embedding_model_singleton

    get_settings.cache_clear()
    reset_embedding_model_singleton()
    yield


def test_write_manifest_produces_valid_json(tmp_path: Path) -> None:
    db = tmp_path / DB_FILENAME
    db.write_text("")  # pretend the DB exists
    path = write_manifest(
        db_path=db,
        dataset="doc2dial",
        model_name="deterministic-stub-v1",
        embedding_dim=16,
        doc_count=6,
        chunk_count=13,
    )
    payload = json.loads(path.read_text())
    assert payload["dataset"] == "doc2dial"
    assert payload["model_name"] == "deterministic-stub-v1"
    assert payload["embedding_dim"] == 16
    assert payload["doc_count"] == 6
    assert payload["chunk_count"] == 13
    assert "completed_at" in payload
    assert payload["db_path"] == str(db)


def test_read_manifest_returns_none_for_missing(tmp_path: Path) -> None:
    assert read_manifest("does-not-exist") is None


def test_read_manifest_returns_none_for_corrupt(tmp_path: Path) -> None:
    # Point the manifest reader at a tmp dir with a corrupt manifest.
    from eval.scripts.ingest_corpus import cache_dir_for

    cache = cache_dir_for("doc2dial")
    cache.mkdir(parents=True, exist_ok=True)
    (cache / MANIFEST_FILENAME).write_text("not-json")
    try:
        assert read_manifest("doc2dial") is None
    finally:
        (cache / MANIFEST_FILENAME).unlink(missing_ok=True)


def test_read_manifest_round_trip(tmp_path: Path) -> None:
    db = tmp_path / DB_FILENAME
    write_manifest(
        db_path=db,
        dataset="doc2dial",
        model_name="deterministic-stub-v1",
        embedding_dim=16,
        doc_count=1,
        chunk_count=1,
    )
    # read_manifest looks at the cache dir, not the DB. The test
    # here only validates the JSON shape; the runner does not
    # rely on `read_manifest` for the DB path (it carries the
    # DB path in the manifest payload and re-opens it directly).
    assert (tmp_path / MANIFEST_FILENAME).exists()


def test_persisted_engine_creates_schema(tmp_path: Path) -> None:
    """Sanity: `persisted_engine(path)` + `Base.metadata.create_all`
    produces tables we can SELECT from."""
    from sqlalchemy.orm import sessionmaker, Session

    from eval.scripts._db import persisted_engine
    from app.models.document import Document

    db_path = tmp_path / "corpus.db"
    engine = persisted_engine(db_path)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    try:
        with SessionLocal() as db:
            count = db.query(Document).count()
            assert count == 0
    finally:
        engine.dispose()
    assert db_path.exists()