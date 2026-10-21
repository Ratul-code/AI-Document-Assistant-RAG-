"""Debug helper: run ONE question through the full retriever + rerank
pipeline and print, for each candidate in the pool, the retriever's
score, the reranker's score, and the reranker's chosen position.

This is the most direct way to verify "is the reranker actually
reordering, or is it silently no-oping?". Run it from `backend/`:

    python -m eval.scripts.debug_rerank_one --question "How do I apply for food stamps?"

By default it picks the 0-th question from the doc2dial dataset. The
output shows the pre-rerank pool (the 30 chunks the retriever surfaced),
their retriever score, the reranker's score for each, and the final
top-K after rerank. If the reranker's column has any variation, the
reranker is alive.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from uuid import UUID

# Bootstrap so we can run this from the repo root.
BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_DIR))


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="debug_rerank_one",
        description=(
            "Run a single question through retriever + rerank and "
            "print side-by-side scores. Use this to verify the "
            "reranker is alive."
        ),
    )
    parser.add_argument("--question", default=None, help="Override the question text.")
    parser.add_argument("--dataset", default="doc2dial")
    parser.add_argument(
        "--embedding-provider",
        choices=["stub", "qwen3"],
        default="qwen3",
    )
    parser.add_argument(
        "--retrieval-mode",
        choices=["dense", "hybrid"],
        default="hybrid",
    )
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--hybrid-dense-k", type=int, default=40)
    parser.add_argument("--hybrid-bm25-k", type=int, default=40)
    parser.add_argument("--hybrid-rrf-k", type=int, default=60)
    parser.add_argument(
        "--pool-multiplier", type=int, default=None,
        help="Override ANALYSIS_RERANK_POOL_MULTIPLIER for this run.",
    )
    parser.add_argument(
        "--rerank-top-k", type=int, default=None,
        help="Override ANALYSIS_RERANK_TOP_K for this run.",
    )
    args = parser.parse_args()

    # Set the embedder BEFORE importing app modules.
    os.environ["EMBEDDING_PROVIDER"] = args.embedding_provider

    # Force a fresh settings + singleton read so the user's .env takes effect.
    from app.core.config import get_settings
    from app.services.ai.embeddings import (
        get_default_embedding_model,
        reset_embedding_model_singleton,
    )
    from app.services.ai.rerankers import (
        get_default_reranker,
        reset_reranker_singleton,
    )

    reset_embedding_model_singleton()
    reset_reranker_singleton()
    get_settings.cache_clear()

    settings = get_settings()
    print("=" * 70)
    print("RERANK DEBUG — one question")
    print("=" * 70)
    print(f"  embedder               : {settings.embedding_provider} -> {get_default_embedding_model().model_name}")
    print(f"  retrieval_mode         : {args.retrieval_mode}")
    print(f"  top_k                  : {args.top_k}")
    print(f"  rerank_enabled         : {settings.analysis_rerank_enabled}")
    print(f"  rerank_provider        : {settings.analysis_rerank_provider}")
    print(f"  rerank_device          : {settings.analysis_rerank_device}")
    print(f"  rerank_timeout_s       : {settings.analysis_rerank_timeout_s}")
    print(f"  rerank_pool_multiplier : {settings.analysis_rerank_pool_multiplier}")
    print(f"  rerank_top_k           : {settings.analysis_rerank_top_k}")
    print()

    reranker = get_default_reranker()
    print(f"  -> Reranker instance: {reranker.name!r} (type={type(reranker).__name__})")
    if not settings.analysis_rerank_enabled:
        print("  WARNING: ANALYSIS_RERANK_ENABLED is false — rerank will NOT run.")
        print("  Set it in .env or override here with ANALYSIS_RERANK_ENABLED=true.")
    print()

    # Load the question from the dataset if not provided.
    if args.question is None:
        from eval.scripts.build_dataset import BACKEND_DIR as _BD
        from eval.schema import Doc2DialDataset
        dataset_path = _BD / "eval" / "datasets" / args.dataset / "dataset.json"
        ds = Doc2DialDataset.model_validate_json(dataset_path.read_text())
        question = ds.questions[0].question
        print(f"Using question[0] from {args.dataset} dataset: {question!r}")
    else:
        question = args.question
        print(f"Using user-supplied question: {question!r}")
    print()

    # Open the persisted corpus.
    from eval.scripts.ingest_corpus import cache_dir_for, read_manifest
    from eval.scripts._db import persisted_engine
    from eval.scripts.run_retrieval_eval import (
        _hybrid_retrieve_for_question,
        _retrieve_for_question,
        _rerank_chunk_ids,
    )
    from eval.scripts.seed_corpus import get_or_create_owner_id
    from sqlalchemy.orm import sessionmaker, Session

    embedder = get_default_embedding_model()
    manifest = read_manifest(args.dataset)
    if manifest is None:
        print("ERROR: no manifest. Run ingest_corpus.py first.", file=sys.stderr)
        return 1
    db_path = Path(manifest["db_path"])
    if not db_path.exists():
        print(f"ERROR: corpus db not found at {db_path}", file=sys.stderr)
        return 1
    engine = persisted_engine(db_path)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    owner_id = get_or_create_owner_id(args.dataset)

    with SessionLocal() as db:
        from app.repositories.embedding_search import fetch_candidate_rows
        all_candidates = fetch_candidate_rows(db, owner_id=owner_id, document_ids=None)
    lookup = {c.chunk_id: c for c in all_candidates}
    print(f"Corpus: {len(all_candidates)} chunks available for rerank lookup.")

    # Run the retriever.
    with SessionLocal() as db:
        t0 = time.time()
        if args.retrieval_mode == "hybrid":
            retrieved_ids = _hybrid_retrieve_for_question(
                db, owner_id=owner_id, scope_document_ids=None,
                question=question, embedder=embedder, top_k=args.top_k,
                min_cosine=0.0,
                dense_top_k=args.hybrid_dense_k, bm25_top_k=args.hybrid_bm25_k,
                rrf_k=args.hybrid_rrf_k,
            )
        else:
            retrieved_ids = _retrieve_for_question(
                db, owner_id=owner_id, scope_document_ids=None,
                question=question, embedder=embedder, top_k=args.top_k,
                min_cosine=0.0,
            )
        retrieve_ms = (time.time() - t0) * 1000
    print()
    print(f"Retriever returned {len(retrieved_ids)} ids in {retrieve_ms:.1f} ms.")
    for i, cid in enumerate(retrieved_ids, start=1):
        row = lookup.get(cid)
        title = row.document_title if row else "?"
        snip = (row.text or "")[:80].replace("\n", " ")
        print(f"  [{i:2d}] {title:30s}  {snip!r}")

    # If rerank is off, stop here.
    if not settings.analysis_rerank_enabled:
        print()
        print("Rerank disabled — nothing to compare.")
        engine.dispose()
        return 0

    # Manually invoke the reranker's rerank() so we can print raw
    # cross-encoder scores side-by-side. This bypasses the eval
    # helper's wrapper so we see exactly what scores the model gave.
    pool_multiplier = (
        args.pool_multiplier
        if args.pool_multiplier is not None
        else settings.analysis_rerank_pool_multiplier
    )
    rerank_top_k = (
        args.rerank_top_k
        if args.rerank_top_k is not None
        else settings.analysis_rerank_top_k
    )
    pool_size = min(len(retrieved_ids), rerank_top_k * max(1, pool_multiplier))
    pool_ids = retrieved_ids[:pool_size]
    pool_rows = [lookup.get(cid) for cid in pool_ids]
    pool_texts = [(r.text if r else "") for r in pool_rows]

    # Raw cross-encoder scores.
    from app.services.document.analyser import rerank_top_rows
    pairs = [(question, t) for t in pool_texts]
    print()
    print(f"Rerank pool: {len(pool_texts)} chunks (top_k={rerank_top_k}, pool_mult={pool_multiplier})")
    print()
    t0 = time.time()
    cross_scores = []
    try:
        # We need a real CrossEncoder to score raw pairs. The
        # CrossEncoderReranker wraps the model, so we use it.
        if hasattr(reranker, "_model"):
            cross_scores = [float(s) for s in reranker._model.predict(
                pairs, batch_size=getattr(reranker, "_batch_size", 16),
                show_progress_bar=False,
            )]
        else:
            # NoneReranker: scores are meaningless.
            cross_scores = [0.0] * len(pairs)
    except Exception as exc:
        print(f"  ERROR scoring pairs: {exc!r}")
        cross_scores = []
    score_ms = (time.time() - t0) * 1000
    print(f"Raw cross-encoder scores (computed in {score_ms:.1f} ms):")
    for i, (row, score) in enumerate(zip(pool_rows, cross_scores)):
        title = row.document_title if row else "?"
        snip = (row.text or "")[:60].replace("\n", " ")
        print(f"  [{i:2d}] score={score:+.4f}  {title:25s}  {snip!r}")
    print()

    # Now run the eval helper to see what it actually picks.
    t0 = time.time()
    reranked_ids = _rerank_chunk_ids(
        ranked_chunk_ids=retrieved_ids,
        question=question,
        rerank_top_k=rerank_top_k,
        pool_multiplier=pool_multiplier,
        timeout_s=settings.analysis_rerank_timeout_s,
        candidate_lookup=lookup.get,
    )
    rerank_ms = (time.time() - t0) * 1000
    print(f"Rerank helper returned {len(reranked_ids)} ids in {rerank_ms:.1f} ms:")
    for i, cid in enumerate(reranked_ids, start=1):
        row = lookup.get(cid)
        title = row.document_title if row else "?"
        snip = (row.text or "")[:80].replace("\n", " ")
        # Find the original position in the retriever output.
        orig_pos = retrieved_ids.index(cid) + 1 if cid in retrieved_ids else "?"
        print(f"  [{i:2d}] (was #{orig_pos})  {title:30s}  {snip!r}")

    # Check if the reranker actually changed the order.
    same = reranked_ids == retrieved_ids[: len(reranked_ids)]
    print()
    if same:
        print("Rerank order == retriever order — the reranker did NOT re-order this question.")
    else:
        moved_up = sum(
            1 for new_pos, cid in enumerate(reranked_ids)
            if cid in retrieved_ids and (retrieved_ids.index(cid) + 1) > new_pos + 1
        )
        moved_down = sum(
            1 for new_pos, cid in enumerate(reranked_ids)
            if cid in retrieved_ids and (retrieved_ids.index(cid) + 1) < new_pos + 1
        )
        print(
            f"Rerank re-ordered the pool: {moved_up} chunks moved up, "
            f"{moved_down} moved down, {len(reranked_ids) - moved_up - moved_down} unchanged."
        )

    engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
