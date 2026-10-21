"""Build the versioned dataset.json + passage_mapping.json.

Reads the persisted corpus that `ingest_corpus.py` produced and
maps each Doc2Dial dialogue turn to its `(document_id, chunk_index)`
gold keys via the fuzzy LCS heuristic. The corpus must already
exist on disk; this script does NOT seed anything.

Doc2Dial flow:
1. Open `backend/eval/.cache/doc2dial/corpus.db` and read its
   chunks indexed by the deterministic doc UUID.
2. For every dialogue turn, the scope is exactly one doc —
   `scope_document_ids = [doc_id]`. No paper-hash indirection
   (Doc2Dial has a stable `doc_id` string per turn).
3. For every reference span (a `(domain, doc_id, sp_id)` triple),
   look up the span text via the spans dict embedded inside each
   `doc2dial_doc.json` document body and run the same fuzzy LCS
   mapping against every chunk of that doc. The `doc_id` is
   namespaced by domain (e.g. `Registrations#3_0` exists in
   `dmv`), so the seeder hashes a composite key
   `"<domain>::<doc_id>"` into the UUID.

The dataset's `scope_mode` is `owner_wide` — the retrieval path
under test searches the whole corpus, not the question's narrowed
scope.

Run from `backend/` with the venv active::

    python -m eval.scripts.build_dataset --dataset doc2dial
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from eval.scripts._db import persisted_engine
from eval.scripts.doc2dial_loader import (
    augment_question,
    cache_dir_for as doc2dial_cache_dir_for,
    filter_standalone_questions,
    is_cached as doc2dial_is_cached,
    load_dialogue_turns,
    load_doc_spans,
)
from eval.scripts.ingest_corpus import (
    DB_FILENAME,
    cache_dir_for as ingest_cache_dir_for,
    read_manifest,
)
from eval.scripts.mapping import build_passage_mapping
from eval.scripts.seed_corpus import (
    BACKEND_DIR,
    deterministic_doc_id,
    doc_text_hash,
    get_or_create_owner_id,
    owner_id_file_for,
)
from eval.schema import (
    Doc2DialDataset,
    MetricsConfig,
    PassageMapping,
    PassageMappingEntry,
    Question,
    RelevantChunkKey,
)


# --------------------------------------------------------------------------
# Question-side helpers
# --------------------------------------------------------------------------


def _normalise_for_hash(text: str) -> str:
    """Whitespace-normalised form, matches `seed_corpus.doc_text_hash`."""
    return re.sub(r"\s+", " ", text or "").strip()


def load_all_turns_doc2dial(dataset: str = "doc2dial") -> list[dict]:
    """Return every Doc2Dial dialogue turn as a list of dicts.

    Shape per row::

        {
            "id":              "doc2dial-<dial_id>-<turn_id>",
            "dialogue_id":     "9f44c153...",
            "turn_id":         3,
            "domain":          "dmv",
            "doc_id":          "Top 5 DMV Mistakes#3_0",  # per-domain id
            "doc_key":         "dmv::Top 5 DMV Mistakes#3_0",  # globally unique
            "question":        "...",
            "question_type":   "dialogue-turn",
            "gold_answer":     "...",
            "references":      [{"sp_id": "4", "label": "precondition"}, ...],
            "evidence_passages": [(sp_id, span_text)],  # resolved from
                                                       # spans[(domain, doc_id, sp_id)]
        }
    """
    if not doc2dial_is_cached(dataset):
        raise FileNotFoundError(
            f"Doc2Dial cache missing at {doc2dial_cache_dir_for(dataset)}. Run:\n"
            f"    python -m eval.scripts.doc2dial_loader"
        )

    turns = load_dialogue_turns(dataset)
    spans = load_doc_spans(dataset)
    out: list[dict] = []
    for t in turns:
        # Resolve evidence passages from the span lookup so the
        # downstream mapper sees a uniform list[(sp_id, span_text)].
        # Doc2Dial references are {sp_id, label}; the doc + domain
        # come from the turn itself. Spans are triple-keyed by
        # `(domain, doc_id, sp_id)` because doc_ids are only unique
        # within a domain.
        domain = t.get("domain", "")
        doc_id = t.get("doc_id", "")
        evidence: list[tuple[str, str]] = []
        for ref in t.get("references", []):
            sp_id = str(ref.get("sp_id", ""))
            if not sp_id:
                continue
            span_text = spans.get((domain, doc_id, sp_id))
            if span_text:
                evidence.append((sp_id, span_text))
        out.append(
            {
                "id": t["id"],
                "dialogue_id": t["dial_id"],
                "turn_id": t["turn_id"],
                "domain": domain,
                "doc_id": doc_id,
                "doc_key": t.get("doc_key", f"{domain}::{doc_id}"),
                "question": t["question"],
                "question_type": t["question_type"],
                "gold_answer": t["answer"],
                "references": t["references"],
                "evidence_passages": evidence,
                # Carried through for `--context-mode` augmentation in
                # `build_question_row_doc2dial`. `augment_question()` reads
                # these and rewrites the question string before the
                # `Question` is built. The gold chunks (`references`,
                # `evidence_passages`) are NOT changed by augmentation —
                # only the string the retriever sees.
                "prev_agent_utterance": t.get("prev_agent_utterance", ""),
                "first_user_utterance": t.get("first_user_utterance", ""),
            }
        )
    return out


# --------------------------------------------------------------------------
# Chunk-side: pull (doc_id, [chunk_text]) from the seeded DB
# --------------------------------------------------------------------------


def load_chunks_by_doc_id(
    db,
    owner_id: UUID,
    *,
    doc_keys: set[str] | None = None,
) -> dict[str, list[tuple[UUID, int, str]]]:
    """Return {doc_uuid_str: [(doc_id, chunk_index, chunk_text)]} for Doc2Dial.

    Doc2Dial's corpus is identified by the deterministic UUID that
    `seed_corpus.deterministic_doc_id(owner_id, doc_key)` produced at
    ingest time. `doc_keys` is the question-first filter — the set
    of `"<domain>::<doc_id>"` strings the chosen turns reference.
    When provided, only docs whose UUID matches one of those keys
    are loaded. When None, every doc owned by `owner_id` is loaded.
    """
    from app.models.document import Document
    from app.models.document_chunk import DocumentChunk
    from app.models.document_page import DocumentPage
    from sqlalchemy import select

    out: dict[str, list[tuple[UUID, int, str]]] = {}
    docs_q = select(Document).where(Document.owner_id == owner_id)
    if doc_keys is not None:
        # Resolve the doc_keys → UUID5 once so the DB only pulls
        # the docs the question slice actually needs.
        from eval.scripts.seed_corpus import deterministic_doc_id
        wanted_uuids = {deterministic_doc_id(owner_id, dk) for dk in doc_keys}
        docs_q = docs_q.where(Document.id.in_(wanted_uuids))
    docs = db.execute(docs_q).scalars().all()
    for doc in docs:
        chunks = db.execute(
            select(DocumentChunk)
            .join(DocumentPage, DocumentChunk.page_id == DocumentPage.id)
            .where(DocumentPage.document_id == doc.id)
            .order_by(DocumentPage.page_number, DocumentChunk.chunk_index)
        ).scalars().all()
        out[str(doc.id)] = [
            (doc.id, c.chunk_index, c.text or "") for c in chunks if c.text
        ]
    return out


# --------------------------------------------------------------------------
# Building one Question row
# --------------------------------------------------------------------------


def build_question_row_doc2dial(
    q: dict,
    doc_id_to_chunks: dict[str, list[tuple[UUID, int, str]]],
    *,
    owner_id: UUID,
    threshold: float = 0.5,
    context_mode: str = "none",
) -> tuple[Question, list[tuple[str, PassageMapping]]]:
    """Resolve one Doc2Dial turn against the seeded corpus.

    Doc2Dial is one doc per dialogue, so this is the only question
    builder:

      * `scope_document_ids` is exactly the dialogue's doc,
        re-derived as a UUID5 over the composite `doc_key`
        `"<domain>::<doc_id>"` (Doc2Dial's per-domain `doc_id`
        is NOT unique across domains, so the composite key is
        what `seed_corpus.load_unique_documents_doc2dial` used).
      * `evidence_passages` already carries the gold span text
        resolved from the `(domain, doc_id, sp_id)` spans dict.
      * The fuzzy LCS mapping is the same one described in
        `mapping.py`.
      * `context_mode` controls how the question string is built
        before going into the `Question` model. `"none"` (default)
        sends the raw user-turn text; `"previous_agent"` and
        `"topic_plus_previous"` prepend prior dialogue context.
        The gold chunks and Doc2Dial `references` are NOT affected
        by context_mode — only the query string changes.
    """
    doc_key = q.get("doc_key", "") or q.get("doc_id", "")
    if not doc_key:
        return (
            Question(
                id=q["id"],
                question=augment_question(q, mode=context_mode),
                question_type=q["question_type"],
                scope_document_ids=[],
                relevant_chunk_keys=[],
                relevant_passage_ids=[],
                gold_answer=q["gold_answer"],
            ),
            [],
        )

    # Re-derive the same UUID5 the seeder used. Doc2Dial's
    # `doc_id` is only unique within a domain, so we hash the
    # composite `doc_key` (`"<domain>::<doc_id>"`) the same way
    # `seed_corpus.load_unique_documents_doc2dial` does at ingest.
    from eval.scripts.seed_corpus import deterministic_doc_id

    doc_id = deterministic_doc_id(owner_id, doc_key)
    chunk_records = doc_id_to_chunks.get(str(doc_id), [])
    evidence_passages: list[tuple[str, str]] = q.get("evidence_passages", [])

    all_relevant: list[RelevantChunkKey] = []
    audit_maps: list[tuple[str, PassageMapping]] = []

    if chunk_records and evidence_passages:
        relevant, mapping = build_passage_mapping(
            question_id=q["id"],
            document_id=doc_id,
            passages=evidence_passages,
            chunks=[(c[1], c[2]) for c in chunk_records],
            threshold=threshold,
            dataset_version="v1",
        )
        if mapping.entries:
            audit_maps.append((q["id"], mapping))
        for key in relevant:
            all_relevant.append(
                RelevantChunkKey(document_id=key[0], chunk_index=key[1])
            )

    return (
        Question(
            id=q["id"],
            question=augment_question(q, mode=context_mode),
            question_type=q["question_type"],
            scope_document_ids=[doc_id] if chunk_records else [],
            relevant_chunk_keys=all_relevant,
            # Reference identifiers are just the per-doc `sp_id`
            # strings; the dialogue's domain + doc_id are already
            # carried on the parent row.
            relevant_passage_ids=[
                str(ref.get("sp_id", "")) for ref in q.get("references", [])
            ],
            gold_answer=q["gold_answer"],
        ),
        audit_maps,
    )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_dataset",
        description=(
            "Build dataset.json + passage_mapping.json from the persisted "
            "corpus that `ingest_corpus.py` produced. Does NOT seed; the "
            "corpus must already exist on disk."
        ),
    )
    parser.add_argument("--dataset", default="doc2dial", choices=["doc2dial"])
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.5,
        help="LCS overlap threshold for passage→chunk mapping.",
    )
    parser.add_argument(
        "--max-questions",
        type=int,
        default=0,
        help="If >0, cap the number of questions (for fast smoke runs).",
    )
    parser.add_argument(
        "--filter-context-dependent",
        action="store_true",
        help=(
            "Drop user turns that are too short or are pure "
            "acknowledgements (e.g. 'yes', 'ok', 'thank you'). Keeps "
            "only the turns that are answerable as standalone "
            "questions. Useful as a single-turn baseline. Gold chunks "
            "are unchanged — only the row count drops. Independent of "
            "`--context-mode`."
        ),
    )
    parser.add_argument(
        "--context-mode",
        default="none",
        choices=["none", "previous_agent", "topic_plus_previous"],
        help=(
            "How to rewrite the user-turn question before sending it to "
            "the retriever. The gold chunks are NEVER changed by this "
            "flag — only the string the retriever sees. "
            "'none' (default): use the raw user-turn text. "
            "'previous_agent': prepend the immediately preceding agent "
            "turn as bracketed context. "
            "'topic_plus_previous': prepend the first user turn of the "
            "dialogue (the topic anchor) plus the immediately preceding "
            "agent turn. Use this to measure retrieval quality under the "
            "same context a chat endpoint would see."
        ),
    )
    args = parser.parse_args(argv)

    owner_id = get_or_create_owner_id(args.dataset)
    print(f"Owner: {owner_id}")

    # 1. Verify the persisted corpus exists.
    manifest = read_manifest(args.dataset)
    if manifest is None:
        print(
            f"ERROR: no manifest at "
            f"backend/eval/.cache/{args.dataset}/manifest.json. "
            f"Run `ingest_corpus.py --dataset {args.dataset}` first.",
            file=sys.stderr,
        )
        return 2
    print(
        f"Persisted corpus: {manifest['doc_count']} docs, "
        f"{manifest['chunk_count']} chunks (model={manifest['model_name']})."
    )

    out_dir = BACKEND_DIR / "eval" / "datasets" / args.dataset
    out_dir.mkdir(parents=True, exist_ok=True)
    assert owner_id_file_for(args.dataset).exists()

    # 2. Open the persisted DB and pull chunks. Doc2Dial indexes
    # chunks by deterministic doc UUID (lookup by UUID5 in
    # `build_question_row_doc2dial`).
    db_path = ingest_cache_dir_for(args.dataset) / DB_FILENAME
    if not db_path.exists():
        print(f"ERROR: corpus DB not found at {db_path}.", file=sys.stderr)
        return 2

    question_rows: list[Question] = []
    audit_by_question: dict[str, list[PassageMapping]] = defaultdict(list)
    scored = 0

    engine = persisted_engine(db_path)
    from sqlalchemy.orm import sessionmaker, Session
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    try:
        with SessionLocal() as db:
            # Doc2Dial: scope is one doc per dialogue; index chunks
            # by doc UUID and use the per-turn evidence_passages.
            # Question-first: slice to --max-questions BEFORE
            # loading chunks, so we only pull chunks for docs the
            # chosen turns actually reference. This keeps the
            # corpus and the dataset in lockstep — the seeder
            # used the same slice to decide which docs to ingest.
            turns = load_all_turns_doc2dial(args.dataset)
            if args.max_questions > 0:
                turns = turns[: args.max_questions]
            print(f"Loaded {len(turns)} turns from {args.dataset}.")
            # Standalone-question filter: drop the obvious
            # no-context cases (short turns, pure acknowledgements)
            # BEFORE we touch the DB. This keeps the chunk fetch
            # scoped to the surviving docs, the same way
            # `--max-questions` does.
            if args.filter_context_dependent:
                before = len(turns)
                turns = filter_standalone_questions(turns)
                print(
                    f"--filter-context-dependent: kept {len(turns)}/"
                    f"{before} standalone-question turns "
                    f"({before - len(turns)} dropped as context-dependent)."
                )
            doc_id_to_chunks = load_chunks_by_doc_id(
                db, owner_id, doc_keys={t["doc_key"] for t in turns}
            )
            print(f"Loaded chunks for {len(doc_id_to_chunks)} docs (of the {len(turns)} turns' source docs).")
            if args.context_mode != "none":
                print(
                    f"--context-mode={args.context_mode}: rewriting each "
                    f"question to prepend prior dialogue context before "
                    f"sending it to the retriever. Gold chunks are "
                    f"unchanged."
                )
            for t in turns:
                try:
                    row, audits = build_question_row_doc2dial(
                        t,
                        doc_id_to_chunks,
                        owner_id=owner_id,
                        threshold=args.threshold,
                        context_mode=args.context_mode,
                    )
                except Exception as exc:
                    print(f"  {t['id']}: skipped ({exc})", file=sys.stderr)
                    continue
                question_rows.append(row)
                for qid, m in audits:
                    audit_by_question[qid].append(m)
                if row.relevant_chunk_keys:
                    scored += 1
    finally:
        engine.dispose()

    # 3. Aggregate stats for the report.
    type_counts: dict[str, int] = defaultdict(int)
    for row in question_rows:
        type_counts[row.question_type] += 1
    dataset = Doc2DialDataset(
        name=args.dataset,
        version="v1",
        status="ready",
        scope_mode="owner_wide",
        passage_mapping="fuzzy_overlap_0.5",
        questions=question_rows,
        metrics_config=MetricsConfig(),
        created_at=datetime.now(timezone.utc),
        owner_id=owner_id,
        source_doc_count=manifest["doc_count"],
        question_type_counts=dict(type_counts),
    )

    dataset_path = out_dir / "dataset.json"
    dataset_path.write_text(dataset.model_dump_json(indent=2))
    mapping_floor = (
        f"{scored}/{len(question_rows)}"
        if len(question_rows) > 0
        else "n/a"
    )
    print(
        f"Wrote {dataset_path} ({len(question_rows)} questions, "
        f"{mapping_floor} mapped to ≥1 chunk — this is the upper "
        f"bound on recall@K regardless of retriever quality)."
    )

    # 5. Audit-only file.
    flat_entries: list[PassageMappingEntry] = []
    for qid, mappings in audit_by_question.items():
        for m in mappings:
            flat_entries.extend(m.entries)
    audit = PassageMapping(
        dataset_version="v1",
        heuristic="fuzzy_overlap_0.5",
        threshold=args.threshold,
        entries=flat_entries,
    )
    mapping_path = out_dir / "passage_mapping.json"
    mapping_path.write_text(audit.model_dump_json(indent=2))
    print(
        f"Wrote {mapping_path} ({len(flat_entries)} entries)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
