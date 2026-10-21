"""Ingest the Doc2Dial corpus into a persisted SQLite once.

This is the **first half** of the eval's "ingest once, score many"
split. It drives the real production pipeline (`parse_document` →
`document._persist_pages` → `document._chunk_pages_for_document` →
`embedding._process_one_document`) for every unique doc in the
dataset, persisting everything to a SQLite file at
`backend/eval/.cache/doc2dial/corpus.db`.

After this script finishes, the corpus lives on disk and
`run_retrieval_eval.py` can score against it without re-seeding —
no parse + chunk + embed pass per evaluation run.

The script writes a `manifest.json` next to the DB so the runner
can verify the embedder name matches without opening the DB:

    {
      "dataset": "doc2dial",
      "model_name": "Qwen/Qwen3-Embedding-0.6B",
      "embedding_dim": 1024,
      "doc_count": 6,
      "chunk_count": 13,
      "completed_at": "2026-10-03T15:00:00+00:00"
    }

The manifest is the source of truth for "did this corpus already
exist?" — re-running with the same embedder is a fast no-op. To
re-seed (e.g. switching embedders), pass `--force` to delete the
existing DB and start over.

Run from `backend/` with the venv active:

    python -m eval.scripts.ingest_corpus \\
        --dataset doc2dial --embedding-provider stub
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from eval.scripts._db import persisted_engine
from eval.scripts.seed_corpus import (
    BACKEND_DIR,
    deterministic_doc_id,
    get_or_create_owner_id,
    load_unique_documents_doc2dial,
    owner_id_file_for,
    seed_one_document,
)


# Manifest schema (kept in sync with the runner's reader):
# the runner refuses to start unless `model_name` matches its
# active embedder — same model-mismatch guard as before, just
# driven by the manifest instead of by `SELECT model_name ...`.
MANIFEST_FILENAME = "manifest.json"
DB_FILENAME = "corpus.db"


def cache_dir_for(dataset: str) -> Path:
    """Where the persisted corpus + manifest live for `dataset`.

    Under `backend/eval/.cache/` which is gitignored. The directory
    is created on demand by `persisted_engine()`.
    """
    return BACKEND_DIR / "eval" / ".cache" / dataset


def write_manifest(
    *,
    db_path: Path,
    dataset: str,
    model_name: str,
    embedding_dim: int,
    doc_count: int,
    chunk_count: int,
    question_count: int | None = None,
) -> Path:
    manifest = {
        "dataset": dataset,
        "model_name": model_name,
        "embedding_dim": embedding_dim,
        "doc_count": doc_count,
        "chunk_count": chunk_count,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "db_path": str(db_path),
    }
    # Question count is recorded so the runner can confirm its
    # `--max-questions` cap matches the corpus's
    # was-built-for-this-many.
    if question_count is not None:
        manifest["question_count"] = question_count
    manifest_path = db_path.parent / MANIFEST_FILENAME
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest_path


def read_manifest(dataset: str) -> dict | None:
    """Return the parsed manifest or None if missing / corrupt."""
    manifest_path = cache_dir_for(dataset) / MANIFEST_FILENAME
    if not manifest_path.exists():
        return None
    try:
        return json.loads(manifest_path.read_text())
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ingest_corpus",
        description=(
            "Ingest the Doc2Dial corpus into a persisted SQLite. "
            "Re-runs are no-ops unless --force is passed."
        ),
    )
    parser.add_argument("--dataset", default="doc2dial", choices=["doc2dial"])
    parser.add_argument(
        "--embedding-provider",
        choices=["stub", "qwen3"],
        default="stub",
        help=(
            "Embedder to use for the seed. The runner refuses to "
            "score against a corpus whose `model_name` does not "
            "match this."
        ),
    )
    parser.add_argument(
        "--max-questions",
        type=int,
        default=40,
        help=(
            "Cap to N user-turns (default 40). The ingested doc "
            "set is derived from those turns — every kept turn's "
            "source doc is included, no popularity filtering."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Delete any existing corpus.db + manifest before re-ingesting.",
    )
    args = parser.parse_args(argv)

    os.environ["EMBEDDING_PROVIDER"] = args.embedding_provider

    from app.core.config import get_settings
    from app.services.ai.embeddings import (
        get_default_embedding_model,
        reset_embedding_model_singleton,
    )

    reset_embedding_model_singleton()
    get_settings.cache_clear()

    cache_dir = cache_dir_for(args.dataset)
    db_path = cache_dir / DB_FILENAME
    manifest_path = cache_dir / MANIFEST_FILENAME

    if args.force:
        if db_path.exists():
            print(f"--force: removing existing {db_path}")
            db_path.unlink()
        if manifest_path.exists():
            manifest_path.unlink()

    if manifest_path.exists() and not args.force:
        existing = read_manifest(args.dataset)
        if existing is not None:
            print(
                f"Corpus already ingested: model={existing['model_name']}, "
                f"docs={existing['doc_count']}, chunks={existing['chunk_count']}"
            )
            print(
                f"  manifest at {manifest_path}. "
                f"Pass --force to re-ingest with a different embedder."
            )
            return 0

    owner_id = get_or_create_owner_id(args.dataset)
    print(f"Owner: {owner_id}")

    # Per-dataset loader. Doc2Dial returns a list of
    # `(digest, document_text)` tuples. The digest is the stable
    # composite `doc_key` (already unique across domains).
    try:
        docs = load_unique_documents_doc2dial(
            args.dataset,
            max_turns=args.max_questions,
        )
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"Found {len(docs)} unique documents in {args.dataset}.")
    print(f"Embedding provider: {args.embedding_provider}")

    # Open the persisted engine with the pubsub patch active so the
    # embedding worker's status-event helper doesn't try to reach
    # Redis (we're still in the eval env, not in the dev DB).
    from eval.scripts._db import patch_pubsub_noop, unpatch_pubsub
    patch_pubsub_noop()
    engine = persisted_engine(db_path)
    from sqlalchemy.orm import sessionmaker, Session
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

    total_pages = 0
    total_chunks = 0
    succeeded = 0
    failed: list[tuple[str, str]] = []

    try:
        model = get_default_embedding_model()
        print(f"Embedder model_name={model.model_name}, dim={model.dim}")

        with SessionLocal() as db:
            for idx, (digest, doc_text) in enumerate(docs, start=1):
                title = f"doc-{idx:03d}"
                try:
                    doc_id, n_pages, n_chunks = seed_one_document(
                        db,
                        owner_id=owner_id,
                        title=title,
                        doc_text=doc_text,
                        # Use a deterministic UUID5 so the IDs in the
                        # persisted `documents` table match the UUID5
                        # keys that `build_dataset.py` later emits into
                        # `dataset.json`. Without this, ingest wrote
                        # random `uuid4()` IDs and the retrieval runner
                        # couldn't resolve any gold chunk → all scores
                        # were 0. See seed_corpus.deterministic_doc_id.
                        doc_id=deterministic_doc_id(owner_id, digest),
                        dataset=args.dataset,
                    )
                    total_pages += n_pages
                    total_chunks += n_chunks
                    succeeded += 1
                    if idx % 100 == 0 or idx == len(docs):
                        print(
                            f"  [{idx}/{len(docs)}] {title}: "
                            f"{n_pages} pages, {n_chunks} chunks"
                        )
                    if idx % 200 == 0:
                        db.commit()
                except Exception as exc:
                    db.rollback()
                    failed.append((title, str(exc)))
                    print(
                        f"  [{idx}/{len(docs)}] {title}: FAILED ({exc})",
                        file=sys.stderr,
                    )
            db.commit()

        manifest_path = write_manifest(
            db_path=db_path,
            dataset=args.dataset,
            model_name=model.model_name,
            embedding_dim=model.dim,
            doc_count=succeeded,
            chunk_count=total_chunks,
            question_count=args.max_questions,
        )

        print(
            f"Ingested {succeeded}/{len(docs)} documents "
            f"({total_pages} pages, {total_chunks} chunks) "
            f"into {db_path}"
        )
        print(f"  manifest at {manifest_path}")
        print(f"  owner_id.txt at {owner_id_file_for(args.dataset)}")
        if failed:
            print(f"  {len(failed)} documents failed:", file=sys.stderr)
            for title, err in failed[:5]:
                print(f"    - {title}: {err[:120]}", file=sys.stderr)
        return 0
    finally:
        engine.dispose()
        unpatch_pubsub()


if __name__ == "__main__":
    sys.exit(main())