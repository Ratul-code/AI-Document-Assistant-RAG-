"""Run a retrieval eval against the persisted corpus + a dataset.json.

This is the eval's only "scoring" entry point. It assumes the corpus
has already been ingested by `ingest_corpus.py` and lives at
`backend/eval/.cache/doc2dial/corpus.db` with a sibling
`manifest.json` recording the embedder's `model_name`.

Steps:

1. Read `manifest.json`. Refuse to start unless the active
   embedder's `model_name` matches — catches "wrong provider
   selected", "stale DB from a different embedder", and "no
   corpus ingested yet" in one check.
2. Open the persisted corpus DB and load `dataset.json`.
3. For every question, embed the natural-language query, call the
   real `fetch_candidate_rows(..., document_ids=...)` and
   `rank_top_rows` retrieval path (per ADR-015: no parallel
   retriever), and score Recall@1/5/10 + MRR via `score.py`.
4. Default scope mode is **`owner_wide`** so the retrieval is
   measured across the whole corpus — what the eval is meant to
   measure. `per_question` is available as an opt-in for
   debugging single questions.
5. Writes a JSON results file (machine-readable) and a Markdown
   report (human-readable). Both go under
   `backend/eval/{results,reports}/`.

Run from `backend/` with the venv active:

    python -m eval.scripts.run_retrieval_eval \\
        --dataset doc2dial --embedding-provider stub
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn
from uuid import UUID

from sqlalchemy import select, tuple_

from eval.schema import Doc2DialDataset
from eval.scripts.ingest_corpus import (
    cache_dir_for,
    read_manifest,
)
from eval.scripts.score import (
    AggregatedMetrics,
    PerQuestionResult,
    aggregate,
    recall_at_k,
)
from eval.scripts.score import (
    mrr as mrr_score,
)

# `cache_dir_for` is re-exported (not called) in this module: the eval
# tests monkeypatch `eval.scripts.run_retrieval_eval.cache_dir_for` to
# point the manifest lookup at a tmp dir. Without this import the
# monkeypatch target does not exist and the patch raises AttributeError.
__all__ = [
    "cache_dir_for",
    "main",
    "verify_corpus_ready",
]

# --------------------------------------------------------------------------
# Pre-flight: corpus + model-mismatch refusal
# --------------------------------------------------------------------------


def _refuse(message: str) -> NoReturn:
    print(f"REFUSE: {message}", file=sys.stderr)
    sys.exit(1)


def verify_corpus_ready(dataset: str, expected_model_name: str) -> dict:
    """Refuse to start unless the persisted corpus exists and matches the embedder.

    Returns the parsed manifest on success. The manifest's `model_name`
    is the source of truth — `corpus.db` itself is not opened here;
    the runner does that after this check passes.
    """
    manifest = read_manifest(dataset)
    if manifest is None:
        _refuse(
            f"No manifest at backend/eval/.cache/{dataset}/manifest.json. "
            f"Run `ingest_corpus.py --dataset {dataset}` first."
        )
    if manifest.get("model_name") != expected_model_name:
        _refuse(
            f"Persisted corpus was ingested with "
            f"model_name={manifest.get('model_name')!r} but the active "
            f"embedder is {expected_model_name!r}. Re-ingest with "
            f"`ingest_corpus.py --dataset {dataset} --force "
            f"--embedding-provider <provider>`."
        )
    return manifest


# --------------------------------------------------------------------------
# Per-question retrieval
# --------------------------------------------------------------------------


def _resolve_relevant_keys_to_chunk_ids(
    db,
    *,
    keys: list,
) -> set[UUID]:
    """Resolve (doc_id, chunk_index) gold keys → chunk UUIDs for scoring.

    The dataset stores `(document_id, chunk_index)` pairs because
    chunk UUIDs are non-deterministic across re-seeds. We resolve
    them here against the persisted DB to get the actual chunk_ids
    the retriever will produce.
    """
    from app.models.document_chunk import DocumentChunk
    from app.models.document_page import DocumentPage

    if not keys:
        return set()
    # Build a {(doc_id, chunk_index): chunk_id} map with one SELECT.
    pairs = [(k.document_id, k.chunk_index) for k in keys]
    rows = db.execute(
        select(DocumentChunk, DocumentPage)
        .join(DocumentPage, DocumentChunk.page_id == DocumentPage.id)
        .where(tuple_(DocumentPage.document_id, DocumentChunk.chunk_index).in_(pairs))
    ).all()
    return {chunk.id for chunk, _page in rows}


def _retrieve_for_question(
    db,
    *,
    owner_id: UUID,
    scope_document_ids: list[UUID] | None,
    question: str,
    embedder,
    top_k: int,
    min_cosine: float,
) -> list[UUID]:
    """Embed + fetch + rank. Returns chunk_ids in descending score order.

    `scope_document_ids=None` means "owner_wide" — every READY doc
    owned by `owner_id` qualifies. An empty list `[]` is invalid:
    `fetch_candidate_rows` rejects it as "user selected nothing". The
    caller is responsible for filtering to questions that have a
    non-empty scope before reaching here (per-question mode).
    """
    from app.repositories.embedding_search import fetch_candidate_rows
    from app.services.document.analyser import rank_top_rows

    candidates = fetch_candidate_rows(
        db,
        owner_id=owner_id,
        document_ids=scope_document_ids,
    )
    if not candidates:
        return []
    query_vec = embedder.embed(question).vector
    # Sanity: refuse on dim mismatch (matches `rank_top_rows`'s invariant).
    if embedder.dim != candidates[0].embedding_dim:
        _refuse(
            f"Query embedder dim={embedder.dim} != candidate "
            f"dim={candidates[0].embedding_dim}. Did you mix embedders?"
        )
    ranked = rank_top_rows(
        candidates,
        query_vec,
        top_k=top_k,
        min_cosine=min_cosine,
    )
    return [row.chunk_id for row, _score in ranked]


def _rerank_chunk_ids(
    *,
    ranked_chunk_ids: list[UUID],
    question: str,
    rerank_top_k: int,
    pool_multiplier: int,
    timeout_s: float,
    candidate_lookup,  # Callable[[UUID], CandidateRow | None]
) -> list[UUID]:
    """Reorder `ranked_chunk_ids` using the configured reranker.

    Used by the eval runner to add a rerank column to the report
    without changing the production retrieval call. Falls back to the
    input order on any reranker failure (mirrors the production
    `rerank_top_rows_async` fallback policy).
    """
    # Map IDs → rows. `candidate_lookup` lets the caller pass a
    # pre-built dict to avoid one DB hit per chunk per question.
    pool_size = min(len(ranked_chunk_ids), rerank_top_k * max(1, pool_multiplier))
    pool_ids = ranked_chunk_ids[:pool_size]
    pool_rows = [candidate_lookup(cid) for cid in pool_ids]
    if any(r is None for r in pool_rows):
        return ranked_chunk_ids[:rerank_top_k]
    from app.services.ai.rerankers import get_default_reranker
    from app.services.document.analyser import rerank_top_rows

    reranker = get_default_reranker()
    try:
        result = rerank_top_rows(
            [(r, 0.0) for r in pool_rows if r is not None],
            question,
            reranker=reranker,
            top_k=rerank_top_k,
            pool_multiplier=pool_multiplier,
        )
        # `result.top_rows` is `list[tuple[CandidateRow, float]]` —
        # the row is the first element, NOT an integer index. Walk
        # it positionally and pull the chunk_id from each row. The
        # prior version destructured with `for idx, _ in result.top_rows`
        # and tried `pool_rows[idx]`, which crashed with
        # `TypeError: list indices must be integers or slices, not CandidateRow`.
        return [row.chunk_id for row, _score in result.top_rows]
    except Exception as exc:  # noqa: BLE001 — eval must not crash on rerank failure
        print(f"  WARN: rerank failed ({exc!r}); using pre-rerank order.", file=sys.stderr)
        return ranked_chunk_ids[:rerank_top_k]


def _hybrid_retrieve_for_question(
    db,
    *,
    owner_id: UUID,
    scope_document_ids: list[UUID] | None,
    question: str,
    embedder,
    top_k: int,
    min_cosine: float,
    dense_top_k: int,
    bm25_top_k: int,
    rrf_k: int,
) -> list[UUID]:
    """Hybrid retrieval: dense top-K + BM25 top-K → RRF → top_k.

    Same scope semantics as `_retrieve_for_question`. Calls the prod
    `hybrid_retrieve` from `app.services.document.retrieval`, which
    internally:
      1. dense top-`dense_top_k` via `rank_top_rows`
      2. BM25 top-`bm25_top_k` via in-memory Okapi BM25
      3. Reciprocal Rank Fusion with `k=rrf_k`
      4. take the top `top_k`

    The BM25 index is built lazily per call (~100 ms on 6k chunks).
    Pass `bm25_index=` from the caller if you want to amortise that
    cost across a batch — see `_run_hybrid_eval_with_shared_index` for
    the loop pattern used by this runner.
    """
    from app.repositories.embedding_search import fetch_candidate_rows
    from app.services.document.retrieval import hybrid_retrieve

    candidates = fetch_candidate_rows(
        db,
        owner_id=owner_id,
        document_ids=scope_document_ids,
    )
    if not candidates:
        return []
    query_vec = embedder.embed(question).vector
    # Sanity: refuse on dim mismatch (same invariant as the dense leg).
    if embedder.dim != candidates[0].embedding_dim:
        _refuse(
            f"Query embedder dim={embedder.dim} != candidate "
            f"dim={candidates[0].embedding_dim}. Did you mix embedders?"
        )
    ranked = hybrid_retrieve(
        candidates,
        query_vec,
        question,
        dense_top_k=dense_top_k,
        bm25_top_k=bm25_top_k,
        rrf_k=rrf_k,
        top_k=top_k,
        min_cosine=min_cosine,
    )
    return [row.chunk_id for row, _score in ranked]


def score_question(
    *,
    question_id: str,
    retrieved_ids: list[UUID],
    relevant_ids: list[UUID],
    top_k: int,
    secondary_top_ks: list[int],
) -> PerQuestionResult:
    """Build the per-question record for `aggregate`.

    `PerQuestionResult` exposes recall_at_{1,5,10} as named fields;
    any top-k value other than one of those (or repeated) is folded
    into `extra_recall_at_k` below.
    """
    all_ks = sorted({top_k, *secondary_top_ks, 1, 5, 10})
    recall_by_k = {k: recall_at_k(retrieved_ids, relevant_ids, k) for k in all_ks}
    return PerQuestionResult(
        question_id=question_id,
        retrieved_ids=retrieved_ids,
        relevant_ids=relevant_ids,
        recall_at_1=recall_by_k[1],
        recall_at_5=recall_by_k[5],
        recall_at_10=recall_by_k[10],
        reciprocal_rank=mrr_score(retrieved_ids, relevant_ids),
    )


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _question_type_breakdown(
    per_question: list[PerQuestionResult],
    dataset: Doc2DialDataset,
) -> dict[str, AggregatedMetrics]:
    """Aggregate per question_type so the report can stratify."""
    by_type: dict[str, list[PerQuestionResult]] = {}
    for row, q in zip(per_question, dataset.questions, strict=False):
        by_type.setdefault(q.question_type, []).append(row)
    return {t: aggregate(rs) for t, rs in by_type.items()}


def _format_metrics(m: AggregatedMetrics) -> str:
    return (
        f"R@1={m.recall_at_1:.3f}  R@5={m.recall_at_5:.3f}  "
        f"R@10={m.recall_at_10:.3f}  MRR={m.mrr:.3f}  "
        f"(scored {m.questions_scored}/{m.questions_total})"
    )


def _miss_counts(rows: list[PerQuestionResult]) -> dict[str, tuple[int, float]]:
    """Per-K miss counts over the scored rows.

    A "miss at K" is a question whose gold set has zero overlap with the
    first K retrieved ids — i.e. recall_at_K == 0. We only count
    questions with non-empty `relevant_ids` (scored rows), because
    `recall_at_K` for empty gold is 0 by convention in `recall_at_k()`,
    which would otherwise inflate the miss count.

    Returns: ``{ "1": (n_missed, pct), "5": (...), "10": (...) }`` over
    the scored-row base.
    """
    scored = [r for r in rows if r.relevant_ids]
    base = len(scored)
    if base == 0:
        return {"1": (0, 0.0), "5": (0, 0.0), "10": (0, 0.0)}
    out: dict[str, tuple[int, float]] = {}
    for k, attr in (("1", "recall_at_1"), ("5", "recall_at_5"), ("10", "recall_at_10")):
        n = sum(1 for r in scored if getattr(r, attr) == 0.0)
        out[k] = (n, n / base)
    return out


def _rerank_deltas(
    pre: list[PerQuestionResult],
    post: list[PerQuestionResult],
) -> dict[str, tuple[int, int, int]]:
    """Rescued/lost tally when comparing retriever-only vs reranked runs.

    Both lists must be aligned by `question_id`. We align by id rather
    than by position because the rerank pass only fills `post` for
    questions where the retriever returned a non-empty list, so the two
    lists may have different lengths.

    Returns: ``{ "1": (n_rescued, n_lost, n_unchanged_hit), ... }``
    where each tuple counts the number of questions whose recall_at_K
    went from 0 to >0 (rescued), >0 to 0 (lost), or stayed >0 / stayed 0
    (unchanged_hit).
    """
    pre_by_id = {r.question_id: r for r in pre}
    post_by_id = {r.question_id: r for r in post}
    common = set(pre_by_id) & set(post_by_id)
    out: dict[str, tuple[int, int, int]] = {}
    for k, attr in (("1", "recall_at_1"), ("5", "recall_at_5"), ("10", "recall_at_10")):
        rescued = lost = unchanged_hit = 0
        for qid in common:
            a = getattr(pre_by_id[qid], attr)
            b = getattr(post_by_id[qid], attr)
            if a == 0.0 and b > 0.0:
                rescued += 1
            elif a > 0.0 and b == 0.0:
                lost += 1
            elif b > 0.0:
                unchanged_hit += 1
        out[k] = (rescued, lost, unchanged_hit)
    return out


def render_report(
    *,
    dataset: Doc2DialDataset,
    metrics: AggregatedMetrics,
    by_type: dict[str, AggregatedMetrics],
    embedder_model_name: str,
    top_k: int,
    scope_mode: str,
    retrieval_mode: str,
    started_at: datetime,
    finished_at: datetime,
    corpus_doc_count: int,
    corpus_chunk_count: int,
    rerank_metrics: AggregatedMetrics | None = None,
    rerank_provider: str | None = None,
    # Full settings block — caller passes every env var that
    # influenced the run, so the report is self-describing.
    hybrid_dense_k: int | None = None,
    hybrid_bm25_k: int | None = None,
    hybrid_rrf_k: int | None = None,
    rerank_top_k: int | None = None,
    rerank_pool_multiplier: int | None = None,
    rerank_model: str | None = None,
    rerank_device: str | None = None,
    rerank_timeout_s: float | None = None,
    rerank_max_length: int | None = None,
    rerank_batch_size: int | None = None,
    snippet_chars: int | None = None,
    min_cosine: float | None = None,
    # Insight metrics — the miss / rescue / lost tallies.
    pre_rerank_rows: list[PerQuestionResult] | None = None,
    post_rerank_rows: list[PerQuestionResult] | None = None,
) -> str:
    lines: list[str] = []
    lines.append(f"# Retrieval Eval — {dataset.name} {dataset.version}")
    lines.append("")
    lines.append(
        f"- Embedder: `{embedder_model_name}`  "
        f"({corpus_doc_count} docs, {corpus_chunk_count} chunks in corpus; "
        f"{len(dataset.questions)} questions in dataset)"
    )
    lines.append(
        f"- Started: {started_at.isoformat(timespec='seconds')}  "
        f"Finished: {finished_at.isoformat(timespec='seconds')}"
    )
    lines.append("")

    # --- Settings (full retrieval + rerank config) ---------------------
    lines.append("## Settings")
    lines.append("")
    lines.append("| knob | value |")
    lines.append("|------|-------|")
    lines.append(f"| scope_mode | `{scope_mode}` |")
    lines.append(f"| retrieval_mode | `{retrieval_mode}` |")
    lines.append(f"| passage_mapping | `{dataset.passage_mapping}` |")
    lines.append(f"| top_k (pool cap) | `{top_k}` |")
    if retrieval_mode == "hybrid":
        lines.append(f"| hybrid.dense_k | `{hybrid_dense_k}` |")
        lines.append(f"| hybrid.bm25_k | `{hybrid_bm25_k}` |")
        lines.append(f"| hybrid.rrf_k | `{hybrid_rrf_k}` |")
    if min_cosine is not None:
        lines.append(f"| min_cosine | `{min_cosine}` |")
    if snippet_chars is not None:
        lines.append(f"| snippet_chars | `{snippet_chars}` |")
    if rerank_metrics is not None and rerank_provider is not None:
        lines.append("| rerank.enabled | `true` |")
        lines.append(f"| rerank.provider | `{rerank_provider}` |")
        if rerank_model is not None:
            lines.append(f"| rerank.model | `{rerank_model}` |")
        if rerank_device is not None:
            lines.append(f"| rerank.device | `{rerank_device}` |")
        if rerank_top_k is not None:
            lines.append(f"| rerank.top_k (LLM input) | `{rerank_top_k}` |")
        if rerank_pool_multiplier is not None:
            lines.append(
                f"| rerank.pool_multiplier | `{rerank_pool_multiplier}` "
                f"(pre-rerank pool ≈ "
                f"`{top_k} × {rerank_pool_multiplier}` = "
                f"`{top_k * rerank_pool_multiplier}`) |"
            )
        if rerank_max_length is not None:
            lines.append(f"| rerank.max_length | `{rerank_max_length}` |")
        if rerank_batch_size is not None:
            lines.append(f"| rerank.batch_size | `{rerank_batch_size}` |")
        if rerank_timeout_s is not None:
            lines.append(f"| rerank.timeout_s | `{rerank_timeout_s}` |")
    lines.append("")

    # --- Overall --------------------------------------------------------
    lines.append("## Overall")
    lines.append("")
    lines.append(f"- Retriever: {_format_metrics(metrics)}")
    if rerank_metrics is not None:
        lines.append("")
        lines.append(f"- **With rerank** (`{rerank_provider}`): {_format_metrics(rerank_metrics)}")
        # Side-by-side delta — the portfolio number.
        d_r1 = rerank_metrics.recall_at_1 - metrics.recall_at_1
        d_r5 = rerank_metrics.recall_at_5 - metrics.recall_at_5
        d_r10 = rerank_metrics.recall_at_10 - metrics.recall_at_10
        d_mrr = rerank_metrics.mrr - metrics.mrr
        lines.append("")
        lines.append(
            f"- **Δ** (rerank − retriever): "
            f"R@1 {d_r1:+.3f}  ·  R@5 {d_r5:+.3f}  ·  "
            f"R@10 {d_r10:+.3f}  ·  MRR {d_mrr:+.3f}"
        )
    lines.append("")

    # --- Miss analysis (the insight block) -----------------------------
    if pre_rerank_rows is not None:
        scored_pre = [r for r in pre_rerank_rows if r.relevant_ids]
        pre_misses = _miss_counts(scored_pre)
        lines.append("## Miss analysis (retriever only)")
        lines.append("")
        lines.append(
            f"Base: **{len(scored_pre)}** scored questions "
            f"(questions with non-empty gold). "
            f"A 'miss' is a question whose gold set has zero overlap "
            f"with the top-K retrieved ids."
        )
        lines.append("")
        lines.append("| K | misses | % of scored |")
        lines.append("|---|--------|-------------|")
        for k in ("1", "5", "10"):
            n, pct = pre_misses[k]
            lines.append(f"| @ {k} | {n} | {pct:.1%} |")
        lines.append("")

    if post_rerank_rows is not None and pre_rerank_rows is not None:
        scored_post = [r for r in post_rerank_rows if r.relevant_ids]
        post_misses = _miss_counts(scored_post)
        deltas = _rerank_deltas(pre_rerank_rows, post_rerank_rows)
        lines.append("## Miss analysis (retriever + rerank)")
        lines.append("")
        lines.append(
            f"Base: **{len(scored_post)}** scored questions (matched "
            f"by id to the retriever-only run)."
        )
        lines.append("")
        lines.append("| K | misses | % of scored |")
        lines.append("|---|--------|-------------|")
        for k in ("1", "5", "10"):
            n, pct = post_misses[k]
            lines.append(f"| @ {k} | {n} | {pct:.1%} |")
        lines.append("")
        lines.append("### Rerank effect: rescued vs lost (per K)")
        lines.append("")
        lines.append(
            "A question is *rescued* if R@K went from 0 (retriever) "
            "to >0 (rerank). *Lost* is the opposite. *Unchanged hit* "
            "means the rerank kept it as a hit. (Questions where the "
            "retriever already had a hit and the rerank preserved it "
            "are not counted as 'lost' even if the exact position "
            "changed.)"
        )
        lines.append("")
        lines.append("| K | rescued | lost | unchanged hit |")
        lines.append("|---|---------|------|---------------|")
        for k in ("1", "5", "10"):
            rescued, lost, unchanged = deltas[k]
            lines.append(f"| @ {k} | {rescued} | {lost} | {unchanged} |")
        lines.append("")

    # --- Per question type ---------------------------------------------
    lines.append("## Per question type")
    lines.append("")
    lines.append("| type | n | R@1 | R@5 | R@10 | MRR |")
    lines.append("|------|---|-----|-----|------|-----|")
    for t, stats in sorted(by_type.items()):
        lines.append(
            f"| {t} | {stats.questions_total} | "
            f"{stats.recall_at_1:.3f} | {stats.recall_at_5:.3f} | "
            f"{stats.recall_at_10:.3f} | {stats.mrr:.3f} |"
        )
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_retrieval_eval",
        description=(
            "Run retrieval-only scoring against an existing dataset.json "
            "and the real production retriever path. The corpus must "
            "have been ingested by `ingest_corpus.py` first."
        ),
    )
    parser.add_argument("--dataset", default="doc2dial", choices=["doc2dial"])
    parser.add_argument(
        "--embedding-provider",
        choices=["stub", "qwen3"],
        default="stub",
        help=(
            "Embedder used to encode the queries at scoring time. "
            "Must match the embedder used at ingest time (the "
            "manifest's `model_name` is checked at startup)."
        ),
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=None,
        help="Override MetricsConfig.top_k. Default uses the dataset's value.",
    )
    parser.add_argument(
        "--scope-mode",
        choices=["per_question", "owner_wide"],
        default="owner_wide",
        help=(
            "`owner_wide` (default) searches every chunk in the "
            "ingested corpus. `per_question` restricts candidates to "
            "the question's `scope_document_ids` (debugging only)."
        ),
    )
    parser.add_argument(
        "--retrieval-mode",
        choices=["dense", "hybrid"],
        default="hybrid",
        help=(
            "`hybrid` (default): dense top-15 + BM25 top-15 fused "
            "via Reciprocal Rank Fusion (k=60). `dense`: cosine-only "
            "(today's v1 behaviour). Both modes return the same final "
            "top-k shape, so the gold-key recall (R@k) and MRR are "
            "directly comparable across modes."
        ),
    )
    parser.add_argument(
        "--hybrid-dense-k",
        type=int,
        default=15,
        help="Dense leg top-K before RRF (hybrid mode only).",
    )
    parser.add_argument(
        "--hybrid-bm25-k",
        type=int,
        default=15,
        help="BM25 leg top-K before RRF (hybrid mode only).",
    )
    parser.add_argument(
        "--hybrid-rrf-k",
        type=int,
        default=60,
        help="RRF constant k (hybrid mode only).",
    )
    parser.add_argument(
        "--max-questions",
        type=int,
        default=0,
        help="If >0, cap to the first N questions (for smoke runs).",
    )
    parser.add_argument(
        "--output-suffix",
        default=None,
        help="Optional filename suffix (default: ISO timestamp).",
    )
    parser.add_argument(
        "--rerank",
        dest="rerank_enabled",
        action="store_true",
        default=None,
        help=(
            "Run an additional rerank pass and write a side-by-side "
            "comparison in the report. By default reads "
            "ANALYSIS_RERANK_ENABLED from the env (and falls back to "
            "False). Explicit --rerank / --no-rerank wins."
        ),
    )
    parser.add_argument(
        "--no-rerank",
        dest="rerank_enabled",
        action="store_false",
        help="Disable the rerank pass (default: env / False).",
    )
    parser.add_argument(
        "--rerank-top-k",
        type=int,
        default=None,
        help=(
            "Override ANALYSIS_RERANK_TOP_K for this eval run only. "
            "Lets you see R@K at the reranker's output size (e.g. "
            "--rerank-top-k 10 to surface R@10 in the rerank column). "
            "Default: read from settings."
        ),
    )
    args = parser.parse_args(argv)

    # Set the embedder BEFORE imports.
    os.environ["EMBEDDING_PROVIDER"] = args.embedding_provider

    # Resolve rerank default from env if not set on the CLI.
    if args.rerank_enabled is None:
        args.rerank_enabled = os.environ.get(
            "ANALYSIS_RERANK_ENABLED",
            "false",
        ).lower() in ("1", "true", "yes")

    from app.core.config import get_settings
    from app.services.ai.embeddings import (
        get_default_embedding_model,
        reset_embedding_model_singleton,
    )
    from eval.scripts._db import persisted_engine
    from eval.scripts.build_dataset import BACKEND_DIR
    from eval.scripts.seed_corpus import get_or_create_owner_id

    reset_embedding_model_singleton()
    get_settings.cache_clear()

    # 1. Manifest check (catches missing/empty/stale corpus).
    embedder = get_default_embedding_model()
    manifest = verify_corpus_ready(args.dataset, embedder.model_name)

    # 2. Open the persisted corpus DB.
    db_path = Path(manifest["db_path"])
    if not db_path.exists():
        _refuse(f"Corpus DB not found at {db_path}. Re-run `ingest_corpus.py`.")
    engine = persisted_engine(db_path)
    from sqlalchemy.orm import Session, sessionmaker

    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

    # 3. Load dataset.json.
    dataset_path = BACKEND_DIR / "eval" / "datasets" / args.dataset / "dataset.json"
    if not dataset_path.exists():
        print(f"ERROR: {dataset_path} not found. Run `build_dataset.py` first.", file=sys.stderr)
        engine.dispose()
        return 2
    dataset = Doc2DialDataset.model_validate_json(dataset_path.read_text())

    top_k = args.top_k if args.top_k is not None else dataset.metrics_config.top_k
    scope_mode = args.scope_mode
    secondary_top_ks = dataset.metrics_config.secondary_top_ks
    min_cosine = dataset.metrics_config.min_cosine

    questions = list(dataset.questions)
    if args.max_questions > 0:
        questions = questions[: args.max_questions]

    if scope_mode == "per_question":
        questions = [q for q in questions if q.scope_document_ids]
        if not questions:
            print("ERROR: no questions with non-empty scope_document_ids.", file=sys.stderr)
            engine.dispose()
            return 2

    owner_id = get_or_create_owner_id(args.dataset)
    print(
        f"Loaded dataset: {len(dataset.questions)} questions, "
        f"top_k={top_k}, scope_mode={scope_mode}, retrieval_mode={args.retrieval_mode}, "
        f"min_cosine={min_cosine}, owner={owner_id}"
    )
    print(
        f"Corpus: {manifest['doc_count']} docs, "
        f"{manifest['chunk_count']} chunks (model={manifest['model_name']})"
    )
    print(f"Embedder: {embedder.model_name}, dim={embedder.dim}")

    # Warm the corpus-wide candidate set in `owner_wide` + `hybrid` mode so the
    # per-question timing reflects retrieval cost only, not candidate-fetch cost.
    # The BM25 index itself is built per-question inside `hybrid_retrieve`.
    if args.retrieval_mode == "hybrid" and scope_mode == "owner_wide":
        from app.repositories.embedding_search import fetch_candidate_rows

        with SessionLocal() as db:
            all_candidates = fetch_candidate_rows(
                db,
                owner_id=owner_id,
                document_ids=None,
            )
        # Warm the corpus-wide candidate set so the per-question timing reflects
        # the retrieval cost only, not the candidate-fetch cost.
        print(
            f"Hybrid: pre-fetched {len(all_candidates)} "
            f"chunks (dense_k={args.hybrid_dense_k}, bm25_k={args.hybrid_bm25_k}, "
            f"rrf_k={args.hybrid_rrf_k})"
        )

    started_at = datetime.now(UTC)
    per_question: list[PerQuestionResult] = []
    per_question_rerank: list[PerQuestionResult] = []

    # When rerank is on we need a chunk_id → CandidateRow map for the
    # reranker to read text out of. Build it once per run from the
    # full corpus candidate set (the same set the hybrid BM25 index
    # was built from, when hybrid+share-raw+rerank).
    rerank_lookup: dict = {}
    if args.rerank_enabled:
        from app.repositories.embedding_search import fetch_candidate_rows

        with SessionLocal() as db:
            all_candidates = fetch_candidate_rows(
                db,
                owner_id=owner_id,
                document_ids=None,
            )
        rerank_lookup = {c.chunk_id: c for c in all_candidates}
        # Reset the reranker singleton so its config picks up any
        # ANALYSIS_RERANK_* env vars set on the CLI.
        from app.services.ai.rerankers import (
            get_default_reranker,
            reset_reranker_singleton,
        )

        reset_reranker_singleton()
        # Print which reranker we're using so the report log is
        # self-explanatory.
        rr = get_default_reranker()
        print(f"Rerank enabled — provider={rr.name}")

    try:
        for idx, q in enumerate(questions, start=1):
            scope_ids = (
                q.scope_document_ids
                if scope_mode == "per_question"
                else None  # owner_wide: pass None so all docs qualify
            )
            with SessionLocal() as db:
                # Resolve the stable (doc_id, chunk_index) gold keys to
                # the chunk UUIDs the retriever actually produces.
                relevant_ids = _resolve_relevant_keys_to_chunk_ids(db, keys=q.relevant_chunk_keys)
                if args.retrieval_mode == "hybrid":
                    retrieved = _hybrid_retrieve_for_question(
                        db,
                        owner_id=owner_id,
                        scope_document_ids=scope_ids,
                        question=q.question,
                        embedder=embedder,
                        top_k=top_k,
                        min_cosine=min_cosine,
                        dense_top_k=args.hybrid_dense_k,
                        bm25_top_k=args.hybrid_bm25_k,
                        rrf_k=args.hybrid_rrf_k,
                    )
                else:
                    retrieved = _retrieve_for_question(
                        db,
                        owner_id=owner_id,
                        scope_document_ids=scope_ids,
                        question=q.question,
                        embedder=embedder,
                        top_k=top_k,
                        min_cosine=min_cosine,
                    )
            row = score_question(
                question_id=q.id,
                retrieved_ids=retrieved,
                relevant_ids=list(relevant_ids),
                top_k=top_k,
                secondary_top_ks=secondary_top_ks,
            )
            per_question.append(row)

            # Optional rerank column.
            if args.rerank_enabled and retrieved:
                # Retrieve the setter settings the reranker needs.
                from app.core.config import get_settings as _gs

                settings = _gs()
                # --rerank-top-k overrides the env value for this
                # eval run only. Lets the user dial R@K reporting
                # independently of the production setting.
                eval_rerank_top_k = (
                    args.rerank_top_k
                    if args.rerank_top_k is not None
                    else settings.analysis_rerank_top_k
                )
                reranked = _rerank_chunk_ids(
                    ranked_chunk_ids=retrieved,
                    question=q.question,
                    rerank_top_k=eval_rerank_top_k,
                    pool_multiplier=settings.analysis_rerank_pool_multiplier,
                    timeout_s=settings.analysis_rerank_timeout_s,
                    candidate_lookup=rerank_lookup.get,
                )
                rerank_row = score_question(
                    question_id=q.id,
                    retrieved_ids=reranked,
                    relevant_ids=list(relevant_ids),
                    top_k=eval_rerank_top_k,
                    secondary_top_ks=secondary_top_ks,
                )
                per_question_rerank.append(rerank_row)

            if idx % 100 == 0:
                # Progress line — only average over rows that have gold
                # so the running R@K reflects the retriever, not
                # mapping failures.
                so_far_scored = [r for r in per_question if r.relevant_ids]
                agg = aggregate(so_far_scored) if so_far_scored else aggregate(per_question)
                print(f"  [{idx}/{len(questions)}] {_format_metrics(agg)}")
    finally:
        engine.dispose()

    finished_at = datetime.now(UTC)

    # Question-first metric. We split the row set into rows that had
    # non-empty gold (scored_rows) and rows with no gold keys
    # (empty_gold_rows). Recall and MRR are only meaningful on
    # rows with gold — averaging over zero-gold rows collapses the
    # metric to 0.0 even when the retriever is fine. The total
    # count is preserved in metrics.questions_total for audit.
    scored_rows = [r for r in per_question if r.relevant_ids]
    empty_gold_rows = [r for r in per_question if not r.relevant_ids]
    if empty_gold_rows:
        print(
            f"  Note: {len(empty_gold_rows)}/{len(per_question)} "
            f"questions had no gold (mapping failed or scope "
            f"missed). Excluded from the metric average."
        )
    metrics = aggregate(scored_rows)
    # Preserve total counts: aggregate() uses len(per_question) for
    # both questions_total and questions_scored by definition.
    # Override questions_total so the report reflects the
    # full iteration count.
    metrics = AggregatedMetrics(
        recall_at_1=metrics.recall_at_1,
        recall_at_5=metrics.recall_at_5,
        recall_at_10=metrics.recall_at_10,
        mrr=metrics.mrr,
        questions_total=len(per_question),
        questions_scored=len(scored_rows),
    )
    by_type = _question_type_breakdown(scored_rows, dataset)

    # Optional rerank aggregate.
    rerank_metrics: AggregatedMetrics | None = None
    rerank_provider: str | None = None
    pre_misses: dict[str, tuple[int, float]] = _miss_counts(scored_rows)
    post_misses: dict[str, tuple[int, float]] | None = None
    deltas: dict[str, tuple[int, int, int]] | None = None
    if args.rerank_enabled and per_question_rerank:
        scored_rerank = [r for r in per_question_rerank if r.relevant_ids]
        rerank_agg = aggregate(scored_rerank) if scored_rerank else aggregate(per_question_rerank)
        rerank_metrics = AggregatedMetrics(
            recall_at_1=rerank_agg.recall_at_1,
            recall_at_5=rerank_agg.recall_at_5,
            recall_at_10=rerank_agg.recall_at_10,
            mrr=rerank_agg.mrr,
            questions_total=len(per_question_rerank),
            questions_scored=len(scored_rerank),
        )
        from app.services.ai.rerankers import get_default_reranker

        rerank_provider = get_default_reranker().name
        post_misses = _miss_counts(scored_rerank)
        deltas = _rerank_deltas(per_question, per_question_rerank)

    report_md = render_report(
        dataset=dataset,
        metrics=metrics,
        by_type=by_type,
        embedder_model_name=embedder.model_name,
        top_k=top_k,
        scope_mode=scope_mode,
        retrieval_mode=args.retrieval_mode,
        started_at=started_at,
        finished_at=finished_at,
        corpus_doc_count=manifest["doc_count"],
        corpus_chunk_count=manifest["chunk_count"],
        rerank_metrics=rerank_metrics,
        rerank_provider=rerank_provider,
        # Full settings block. Rerank tunables come from the live
        # settings so the report reflects what the reranker actually
        # saw (e.g. ANALYSIS_RERANK_TIMEOUT_S).
        hybrid_dense_k=args.hybrid_dense_k if args.retrieval_mode == "hybrid" else None,
        hybrid_bm25_k=args.hybrid_bm25_k if args.retrieval_mode == "hybrid" else None,
        hybrid_rrf_k=args.hybrid_rrf_k if args.retrieval_mode == "hybrid" else None,
        snippet_chars=get_settings().analysis_snippet_chars,
        min_cosine=min_cosine,
        rerank_top_k=(
            args.rerank_top_k
            if args.rerank_top_k is not None
            else get_settings().analysis_rerank_top_k
        ),
        rerank_pool_multiplier=get_settings().analysis_rerank_pool_multiplier,
        rerank_model=get_settings().analysis_rerank_model,
        rerank_device=get_settings().analysis_rerank_device,
        rerank_timeout_s=get_settings().analysis_rerank_timeout_s,
        rerank_max_length=get_settings().analysis_rerank_max_length,
        rerank_batch_size=get_settings().analysis_rerank_batch_size,
        # Insight metrics — feeds the miss / rescue / lost tables.
        pre_rerank_rows=per_question,
        post_rerank_rows=per_question_rerank if per_question_rerank else None,
    )

    embedder_slug = embedder.model_name.replace("/", "_").lower()
    scope_slug = scope_mode
    mode_slug = args.retrieval_mode
    suffix = args.output_suffix or finished_at.strftime("%Y%m%dT%H%M%S")
    results_path = (
        BACKEND_DIR
        / "eval"
        / "results"
        / f"{args.dataset}_{embedder_slug}_{scope_slug}_{mode_slug}_{suffix}.json"
    )
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(
        json.dumps(
            {
                "dataset": args.dataset,
                "version": dataset.version,
                "embedder": embedder.model_name,
                "top_k": top_k,
                "scope_mode": scope_mode,
                "retrieval_mode": args.retrieval_mode,
                "hybrid_params": (
                    {
                        "dense_k": args.hybrid_dense_k,
                        "bm25_k": args.hybrid_bm25_k,
                        "rrf_k": args.hybrid_rrf_k,
                    }
                    if args.retrieval_mode == "hybrid"
                    else None
                ),
                "corpus": {
                    "doc_count": manifest["doc_count"],
                    "chunk_count": manifest["chunk_count"],
                    "manifest_completed_at": manifest["completed_at"],
                },
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "metrics": {
                    "recall_at_1": metrics.recall_at_1,
                    "recall_at_5": metrics.recall_at_5,
                    "recall_at_10": metrics.recall_at_10,
                    "mrr": metrics.mrr,
                    "questions_total": metrics.questions_total,
                    "questions_scored": metrics.questions_scored,
                    "empty_gold_skipped": (metrics.questions_total - metrics.questions_scored),
                },
                "rerank": (
                    {
                        "enabled": True,
                        "provider": rerank_provider,
                        "rerank_top_k": (
                            args.rerank_top_k
                            if args.rerank_top_k is not None
                            else get_settings().analysis_rerank_top_k
                        ),
                        "metrics": {
                            "recall_at_1": rerank_metrics.recall_at_1,
                            "recall_at_5": rerank_metrics.recall_at_5,
                            "recall_at_10": rerank_metrics.recall_at_10,
                            "mrr": rerank_metrics.mrr,
                            "questions_total": rerank_metrics.questions_total,
                            "questions_scored": rerank_metrics.questions_scored,
                            "empty_gold_skipped": (
                                rerank_metrics.questions_total - rerank_metrics.questions_scored
                            ),
                            "delta": {
                                "recall_at_1": (rerank_metrics.recall_at_1 - metrics.recall_at_1),
                                "recall_at_5": (rerank_metrics.recall_at_5 - metrics.recall_at_5),
                                "recall_at_10": (
                                    rerank_metrics.recall_at_10 - metrics.recall_at_10
                                ),
                                "mrr": rerank_metrics.mrr - metrics.mrr,
                            },
                        },
                        "miss_analysis": {
                            "retriever_only": {
                                str(k): {
                                    "misses": pre_misses[k][0],
                                    "miss_pct": pre_misses[k][1],
                                }
                                for k in ("1", "5", "10")
                            },
                            "retriever_plus_rerank": {
                                str(k): {
                                    "misses": post_misses[k][0],
                                    "miss_pct": post_misses[k][1],
                                }
                                for k in ("1", "5", "10")
                            },
                            "rerank_effect": {
                                str(k): {
                                    "rescued": deltas[k][0],
                                    "lost": deltas[k][1],
                                    "unchanged_hit": deltas[k][2],
                                }
                                for k in ("1", "5", "10")
                            },
                        },
                    }
                    if rerank_metrics is not None
                    else {"enabled": False}
                ),
                "per_question": [
                    {
                        "question_id": r.question_id,
                        "retrieved_ids": [str(c) for c in r.retrieved_ids],
                        "relevant_ids": [str(c) for c in r.relevant_ids],
                        "recall_at_1": r.recall_at_1,
                        "recall_at_5": r.recall_at_5,
                        "recall_at_10": r.recall_at_10,
                        "reciprocal_rank": r.reciprocal_rank,
                    }
                    for r in per_question
                ],
            },
            indent=2,
        )
    )

    report_path = (
        BACKEND_DIR
        / "eval"
        / "reports"
        / f"{args.dataset}_{embedder_slug}_{scope_slug}_{mode_slug}_{suffix}.md"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_md)

    print(f"\nWrote {results_path} and {report_path}\nOverall: {_format_metrics(metrics)}")
    if rerank_metrics is not None and rerank_provider is not None:
        # Print the rerank column + delta so the rerank effect is
        # visible on stdout — otherwise the only signal is buried
        # inside the JSON, which is easy to miss.
        d_r1 = rerank_metrics.recall_at_1 - metrics.recall_at_1
        d_r5 = rerank_metrics.recall_at_5 - metrics.recall_at_5
        d_r10 = rerank_metrics.recall_at_10 - metrics.recall_at_10
        d_mrr = rerank_metrics.mrr - metrics.mrr
        print(f"With rerank ({rerank_provider}): {_format_metrics(rerank_metrics)}")
        print(
            f"Delta (rerank − retriever): "
            f"R@1 {d_r1:+.3f}  ·  R@5 {d_r5:+.3f}  ·  "
            f"R@10 {d_r10:+.3f}  ·  MRR {d_mrr:+.3f}"
        )
        if deltas is not None:
            d1 = deltas["1"]
            d5 = deltas["5"]
            print(
                f"Rerank effect (per K): "
                f"@1 rescued={d1[0]} lost={d1[1]} · "
                f"@5 rescued={d5[0]} lost={d5[1]}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
