"""End-to-end test for the eval pipeline (hermetic, stub embedder).

Drives the full `seed_one_document` → `fetch_candidate_rows` →
`rank_top_rows` → scoring loop with three hand-written "docs"
and exercises:

* **Per-question scope** retrieval (legacy test).
* **Owner-wide retrieval** against a *persisted* SQLite (the new
  default — the runner no longer seeds in-memory).
* **Manifest-based model-mismatch refusal** (replaces the old
  `verify_embedder_match` DB-group-by query).

Why three docs and not 50? Stub embedder is content-agnostic, so
*any* doc > top_k would dilute recall with random hits. Picking a
small fixture keeps the test fast (<2s) and the recall assertion
deterministic.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from eval.scripts._db import build_hermetic_env, persisted_engine
from eval.scripts.ingest_corpus import write_manifest
from eval.scripts.run_retrieval_eval import (
    _retrieve_for_question,
    score_question,
    verify_corpus_ready,
)
from eval.scripts.score import PerQuestionResult, aggregate
from eval.scripts.seed_corpus import (
    deterministic_doc_id,
    get_or_create_owner_id,
    seed_one_document,
)

# --------------------------------------------------------------------------
# Tiny fixture corpus — three short docs with enough overlap to be
# distinguishable by a content-aware model (Qwen3) and completely
# indistinguishable by SHA-based stub (intentional — stub is for
# pipeline testing, not accuracy).
# --------------------------------------------------------------------------

DOC_A = (
    "Title: T-cell recovery in HAdV-55 patients\n\n"
    "Passage: T-cell counts were significantly higher in patients "
    "who recovered from severe infection.\n\n"
    "Passage: Lower T-cell counts correlated with worse outcomes.\n"
)

DOC_B = (
    "Title: Adenovirus type 7 versus type 3 severity\n\n"
    "Passage: Patients with HAdV-7 had lower white blood cell counts "
    "compared to HAdV-3 patients.\n\n"
    "Passage: Imaging data was collected on admission and during "
    "hospitalization.\n"
)

DOC_C = (
    "Title: Sero-surveys of MERS-CoV in Saudi Arabia\n\n"
    "Passage: Cross-sectional sero-surveys detected MERS-CoV "
    "antibodies in 2.3% of the general population.\n\n"
    "Passage: Camel-exposed workers had significantly higher "
    "seroprevalence than matched controls.\n"
)

DOCS = [
    ("doc-a", DOC_A),
    ("doc-b", DOC_B),
    ("doc-c", DOC_C),
]


def _seed_three_docs(SessionLocal, owner_id: UUID) -> dict[str, UUID]:
    """Seed DOC_A/B/C into the *given* session's engine.

    Return {doc_digest: doc_id}. Caller owns the engine.
    """
    import hashlib

    doc_id_map: dict[str, UUID] = {}
    with SessionLocal() as db:
        for title, doc_text in DOCS:
            norm = " ".join(doc_text.split())
            digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()
            doc_id_for_seed = deterministic_doc_id(owner_id, digest)
            doc_id, _pages, _chunks = seed_one_document(
                db,
                owner_id=owner_id,
                title=title,
                doc_text=doc_text,
                doc_id=doc_id_for_seed,
            )
            doc_id_map[digest] = doc_id
        db.commit()
    return doc_id_map


def _seed_persisted_with_manifest(
    db_path: Path, owner_id: UUID, *, model_name: str
) -> dict[str, UUID]:
    """Seed DOC_A/B/C into a fresh persisted SQLite at `db_path`,
    then write a manifest next to it."""
    from sqlalchemy.orm import Session, sessionmaker

    engine = persisted_engine(db_path)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    try:
        doc_id_map = _seed_three_docs(SessionLocal, owner_id)
    finally:
        engine.dispose()
    write_manifest(
        db_path=db_path,
        dataset="test_fixture",
        model_name=model_name,
        embedding_dim=16,
        doc_count=len(DOCS),
        chunk_count=sum(1 for _ in doc_id_map),
    )
    return doc_id_map


@pytest.fixture(autouse=True)
def _stub_embedder():
    """Force the stub embedder for the whole module."""
    os.environ["EMBEDDING_PROVIDER"] = "stub"
    from app.core.config import get_settings
    from app.services.ai.embeddings import reset_embedding_model_singleton

    get_settings.cache_clear()
    reset_embedding_model_singleton()
    yield


# --------------------------------------------------------------------------
# Per-question scope (legacy path, kept for backward-compat coverage)
# --------------------------------------------------------------------------


def test_retrieve_with_stub_returns_all_scope_chunks() -> None:
    owner_id = get_or_create_owner_id()
    with build_hermetic_env() as (_, SessionLocal):
        doc_id_map = _seed_three_docs(SessionLocal, owner_id)
        doc_a = doc_id_map[next(iter(doc_id_map))]

        from app.services.ai.embeddings import get_default_embedding_model

        embedder = get_default_embedding_model()
        with SessionLocal() as db:
            retrieved = _retrieve_for_question(
                db,
                owner_id=owner_id,
                scope_document_ids=[doc_a],
                question="What did the T-cell study find?",
                embedder=embedder,
                top_k=5,
                min_cosine=0.0,
            )
            assert len(retrieved) == 1, f"expected 1 chunk, got {len(retrieved)}"


def test_retrieve_owner_wide_returns_chunks_from_every_doc() -> None:
    """Owner-wide scope (no document_ids) returns chunks across the whole corpus."""
    owner_id = get_or_create_owner_id()
    with build_hermetic_env() as (_, SessionLocal):
        _seed_three_docs(SessionLocal, owner_id)

        from app.services.ai.embeddings import get_default_embedding_model

        embedder = get_default_embedding_model()
        with SessionLocal() as db:
            retrieved = _retrieve_for_question(
                db,
                owner_id=owner_id,
                scope_document_ids=None,  # owner_wide
                question="anything",
                embedder=embedder,
                top_k=5,
                min_cosine=0.0,
            )
            # One chunk per doc (stub + 3 short docs => 1 chunk each).
            assert len(retrieved) == 3, f"expected 3 chunks, got {len(retrieved)}"


# --------------------------------------------------------------------------
# Scoring primitives (no DB)
# --------------------------------------------------------------------------


def test_score_question_aggregates_correctly() -> None:
    """Pure scoring logic with hand-crafted retrieval orderings."""
    qid = "test-q1"
    relevant = [UUID(int=1), UUID(int=2)]
    retrieved = [UUID(int=3), UUID(int=1), UUID(int=4), UUID(int=2)]
    row = score_question(
        question_id=qid,
        retrieved_ids=retrieved,
        relevant_ids=relevant,
        top_k=5,
        secondary_top_ks=[1, 10],
    )
    # Recall@1 = 0 (first hit not relevant), Recall@5 = 1.0 (both
    # appear in top 5), MRR = 0.5 (first relevant at position 2).
    assert row.recall_at_1 == 0.0
    assert row.recall_at_5 == 1.0
    assert row.recall_at_10 == 1.0
    assert row.reciprocal_rank == 0.5


def test_aggregate_across_questions() -> None:
    rows = [
        PerQuestionResult(
            question_id=f"q{i}",
            retrieved_ids=[UUID(int=i), UUID(int=100 + i)],
            relevant_ids=[UUID(int=i)],
            recall_at_1=1.0,
            recall_at_5=1.0,
            recall_at_10=1.0,
            reciprocal_rank=1.0,
        )
        for i in range(1, 4)
    ]
    agg = aggregate(rows)
    assert agg.questions_total == 3
    assert agg.questions_scored == 3
    assert agg.recall_at_5 == 1.0
    assert agg.mrr == 1.0


# --------------------------------------------------------------------------
# Manifest-based model-mismatch refusal (replaces DB-group-by check).
# The runner refuses to start unless the persisted corpus's `model_name`
# matches the active embedder.
# --------------------------------------------------------------------------


def test_verify_corpus_ready_accepts_matching_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_id = get_or_create_owner_id()
    db_path = tmp_path / "corpus.db"
    # Set the EMBEDDING_PROVIDER env so `verify_corpus_ready` reads it
    # indirectly via the active embedder. We monkeypatch
    # `cache_dir_for` so the manifest lives under tmp_path.
    from app.services.ai.embeddings import get_default_embedding_model

    embedder = get_default_embedding_model()

    monkeypatch.setattr(
        "eval.scripts.run_retrieval_eval.read_manifest",
        lambda dataset: (
            json.loads((tmp_path / "manifest.json").read_text())
            if (tmp_path / "manifest.json").exists()
            else None
        ),
    )
    monkeypatch.setattr(
        "eval.scripts.run_retrieval_eval.cache_dir_for",
        lambda dataset: tmp_path,
    )
    _seed_persisted_with_manifest(db_path, owner_id, model_name=embedder.model_name)
    # Manifest is at tmp_path/manifest.json — same dir as corpus.db.
    manifest = verify_corpus_ready("test_fixture", embedder.model_name)
    assert manifest["model_name"] == embedder.model_name


def test_verify_corpus_ready_refuses_missing_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.ai.embeddings import get_default_embedding_model

    embedder = get_default_embedding_model()
    monkeypatch.setattr(
        "eval.scripts.run_retrieval_eval.read_manifest",
        lambda dataset: None,
    )
    with pytest.raises(SystemExit) as exc_info:
        verify_corpus_ready("test_fixture", embedder.model_name)
    assert exc_info.value.code == 1


def test_verify_corpus_ready_refuses_model_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.ai.embeddings import get_default_embedding_model

    embedder = get_default_embedding_model()
    monkeypatch.setattr(
        "eval.scripts.run_retrieval_eval.read_manifest",
        lambda dataset: {
            "model_name": "totally-bogus-embedder",
            "doc_count": 0,
            "chunk_count": 0,
        },
    )
    with pytest.raises(SystemExit) as exc_info:
        verify_corpus_ready("test_fixture", embedder.model_name)
    assert exc_info.value.code == 1
