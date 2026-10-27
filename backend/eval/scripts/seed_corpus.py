"""Helpers for parsing + chunking + embedding the eval corpus.

This is **not** an entry-point script anymore. The CLI was replaced by
`ingest_corpus.py`, which writes to a *persisted* SQLite at
`backend/eval/.cache/doc2dial/corpus.db` (under `.gitignore`).
The runners that consume the corpus (`build_dataset.py` and
`run_retrieval_eval.py`) read the persisted DB instead of
re-seeding every invocation.

Per-dataset helpers (one for the supported dataset):
* `load_unique_documents_doc2dial(...)`  — Doc2Dial: JSON cache

Shared helpers (dataset-agnostic):
* `seed_one_document(db, *, owner_id, title, doc_text, doc_id=...)` —
  run the production pipeline (`parse_document` →
  `document._persist_pages` → `document._chunk_pages_for_document`
  → `embedding._process_one_document`) for a single document.
* `get_or_create_owner_id(dataset)` — read or write the per-dataset
  `owner_id.txt`. The dataset has its own owner UUID so `owner_wide`
  retrieval never mixes corpora.
* `deterministic_doc_id(owner_id, doc_digest)` — UUID5 used to make
  `Document.id` stable across re-ingests (ADR-016).
* `doc_text_hash(...)` — content-deterministic dedup key, shared with
  `build_dataset.py` so question hashes match the corpus hashes
  byte-for-byte.
* `BACKEND_DIR` — path constant consumed by every other eval script.

All heavy imports stay inside the call sites (no app imports at
module top level) so `--help` and the test fixture can `import`
this module without paying the full app startup cost.
"""

from __future__ import annotations

import hashlib
import re
import tempfile
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from eval.scripts.doc2dial_loader import (
    cache_dir_for as doc2dial_cache_dir_for,
)
from eval.scripts.doc2dial_loader import (
    cap_by_turns as doc2dial_cap_by_turns,
)
from eval.scripts.doc2dial_loader import (
    is_cached as doc2dial_is_cached,
)
from eval.scripts.doc2dial_loader import (
    load_dialogue_turns as doc2dial_load_dialogue_turns,
)
from eval.scripts.doc2dial_loader import (
    load_documents as doc2dial_load_documents,
)

BACKEND_DIR = Path(__file__).resolve().parents[2]  # backend/eval/scripts/seed_corpus.py


def owner_id_file_for(dataset: str) -> Path:
    """Path to the per-dataset `owner_id.txt`.

    The dataset has its own owner UUID so `owner_wide` retrieval
    scopes are isolated. The file is created on first read.
    """
    return BACKEND_DIR / "eval" / "datasets" / dataset / "owner_id.txt"


# --------------------------------------------------------------------------
# Owner-id persistence
# --------------------------------------------------------------------------


def get_or_create_owner_id(dataset: str = "doc2dial") -> UUID:
    """Read or create the synthetic eval owner UUID for `dataset`.

    Persisted to a per-dataset `.txt` file so the same owner is used
    across re-runs of the same dataset. Previously-written
    `dataset.json` files continue to reference the correct owner and
    stay valid.
    """
    path = owner_id_file_for(dataset)
    if path.exists():
        text = path.read_text().strip()
        try:
            return UUID(text)
        except ValueError:
            # Corrupted — overwrite.
            pass
    new_id = uuid4()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(new_id))
    return new_id


# --------------------------------------------------------------------------
# Doc2Dial document loading
# --------------------------------------------------------------------------


def doc_text_hash(text: str) -> str:
    """Stable dedup key for a document's text. SHA-256 of the text.

    Used by Doc2Dial's document loader so the same digest for two
    paragraphs means the same content under the same owner.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def deterministic_doc_id(owner_id: UUID, doc_digest: str) -> UUID:
    """Return a stable UUID5 for `(owner_id, doc_digest)`.

    The persisted SQLite is rebuilt by `ingest_corpus.py` on demand
    (with `--force`). Without a deterministic ID, the `Document.id`
    UUIDs would change on re-ingest, which would invalidate the
    `scope_document_ids` stored in `dataset.json`. (Not currently
    used by `owner_wide` scoring, but kept consistent so the same
    document hash always produces the same `Document.id` — useful for
    audit and for switching back to `per_question` scope.)
    """
    APP_NAMESPACE = UUID("8d3a1c6f-2f8e-4b29-9d18-2fa5b9b7e1c1")
    return uuid5(APP_NAMESPACE, f"{owner_id}:{doc_digest}")


def _normalise_for_hash(text: str) -> str:
    """Whitespace-only normalisation for the dedup hash."""
    return re.sub(r"\s+", " ", text).strip()


def load_unique_documents_doc2dial(
    dataset: str = "doc2dial",
    *,
    max_turns: int = 40,
) -> list[tuple[str, str]]:
    """Return [(doc_id, doc_text)] for the question-first Doc2Dial subset.

    Reads the cached JSON, takes the first `max_turns` user turns
    (stable order), and returns one entry per doc referenced by
    those turns. The cap is applied at the turn level (not the doc
    level) so the doc set is exactly the set that backs the
    questions the runner will score.

    Call sites:

      * `ingest_corpus.py` forwards the user's `--max-questions` here
        and ingests the returned docs.
      * `build_dataset.py` independently applies the same cap via
        `--max-questions` on the turn list, so the question list
        and the doc list stay in lockstep.
    """
    if not doc2dial_is_cached(dataset):
        raise FileNotFoundError(
            f"Doc2Dial cache missing at {doc2dial_cache_dir_for(dataset)}. "
            f"Run:\n"
            f"    python -m eval.scripts.doc2dial_loader"
        )

    documents = doc2dial_load_documents(dataset)
    turns = doc2dial_load_dialogue_turns(dataset)
    documents_kept, _turns_kept = doc2dial_cap_by_turns(turns, documents, max_turns=max_turns)
    # Each entry is (doc_id, {"title": ..., "doc_text": ..., ...});
    # we return the doc_text field so the rest of the pipeline
    # treats Doc2Dial docs as plain markdown bodies.
    return [(doc_id, body.get("doc_text", "")) for doc_id, body in documents_kept]


# --------------------------------------------------------------------------
# Seeding loop
# --------------------------------------------------------------------------


def seed_one_document(
    db,
    *,
    owner_id: UUID,
    title: str,
    doc_text: str,
    doc_id: UUID | None = None,
    dataset: str = "doc2dial",
) -> tuple[UUID, int, int]:
    """Run the full parse+chunk+embed pipeline for one document.

    Returns `(document_id, n_pages, n_chunks)`. Embeddings are
    produced by the active EmbeddingModel (set via
    `EMBEDDING_PROVIDER` in env).

    `doc_id` is optional. When supplied (UUID5 derived from
    `(owner_id, doc_text_hash)`), the same `Document.id` is reused
    across re-ingest invocations so the persisted DB stays
    consistent with `dataset.json`'s UUIDs.

    `dataset` controls the per-dataset `storage_path` prefix and the
    synthetic user's `email` / `name` so the corpus is visibly
    separate in the dev `users` table.
    """
    from app.models.document import Document, DocumentStatus
    from app.models.user import User
    from app.services.document.parser import parse_document
    from app.tasks import document as document_task
    from app.tasks import embedding as embedding_task

    # 1. Write the doc to a temp .md file.
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(doc_text)
        tmp_path = Path(fh.name)

    try:
        # 2. Parse the doc.
        parsed = parse_document(tmp_path, "text/markdown", filename=f"{title}.md")
        n_pages = len(parsed)

        # 3. Ensure the user exists (idempotent — INSERT OR IGNORE).
        #    No async session available here, so use sync SQL.
        from sqlalchemy import select

        existing = db.execute(select(User).where(User.id == owner_id)).scalar_one_or_none()
        if existing is None:
            user = User(
                id=owner_id,
                email=f"eval+{dataset}@local.invalid",
                name=f"Eval Owner ({dataset})",
                hashed_password="x" * 60,  # bcrypt-length placeholder
                is_active=True,
            )
            db.add(user)
            db.flush()

        # 4. Create the document row. Use sync — same session.
        doc_id = doc_id or uuid4()
        doc = Document(
            id=doc_id,
            owner_id=owner_id,
            title=title,
            filename=f"{title}.md",
            mime_type="text/markdown",
            file_size_bytes=len(doc_text.encode("utf-8")),
            storage_bucket="eval-cache",
            storage_path=f"{dataset}/{doc_id}.md",
            status=DocumentStatus.processing,
        )
        db.add(doc)
        db.flush()

        # 5. Persist pages (sync helper from app.tasks.document).
        document_task._persist_pages(db, doc_id, parsed)

        # 6. Chunk pages.
        n_chunks = document_task._chunk_pages_for_document(db, doc_id, owner_id)

        # 7. Embed all chunks + flip to READY. The embedding helper
        #    sets status=ready (or failed) at the end.
        embedding_task._process_one_document(db, doc_id, owner_id)

        return doc_id, n_pages, n_chunks
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass
