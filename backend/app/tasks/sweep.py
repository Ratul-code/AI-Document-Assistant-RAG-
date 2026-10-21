"""Periodic sweep / cleanup Celery tasks.

`sweep_stuck_learning_documents` is the recovery backstop for the
embedding pipeline. A document is "stuck in LEARNING" when parsing +
chunking finished but the embedding worker never finalized it. Three
ways this can happen:

  * `process_document` raised before it could `send_task` the embedding
    job. The Celery task retries (`max_retries=3`) and then marks the
    doc FAILED — but only on a *final* exception. If the worker process
    is killed mid-run (OOM, container restart) nothing flips the row.
  * The Celery broker was unreachable when `process_document` tried to
    `send_task`. The exception is logged and swallowed (so the
    upload-confirm endpoint still returns 200 to the client), and the
    doc sits in LEARNING forever.
  * The embedding Celery worker process died mid-run, between writing
    some embeddings and calling `_finalize_document_status`. The doc
    stays LEARNING even though most chunks may have embeddings.

This sweep fixes all three: it scans for LEARNING docs whose `updated_at`
is older than a threshold, re-enqueues `embed_chunks_for_document` for
each, and lets the (idempotent) worker pick up where it left off.

The sweep itself is fire-and-forget — it logs and moves on if a single
re-enqueue fails. Worst case the next sweep tick retries.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from celery import shared_task
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.celery_app import celery_app  # noqa: F401 -- keeps shared_task bound to our app
from app.db.session import get_session_factory
from app.repositories.document import find_stuck_learning

logger = logging.getLogger(__name__)


# Threshold: how old (since `updated_at`) a LEARNING doc must be before
# the sweep considers it stuck. Set comfortably longer than the worst-
# case runtime of `embed_chunks_for_document` (15-min hard time limit
# in `app/core/celery_app.py`) plus its 3 retries, so we never race the
# primary pipeline. 30 minutes is a safe default; tune via env if
# embeddings get slower.
DEFAULT_STUCK_THRESHOLD_MINUTES = 30


async def _find_stuck_documents(threshold: datetime) -> list:
    """Async helper that opens its own session and returns stuck doc rows."""
    session_factory = get_session_factory()
    async with session_factory() as db:  # type: AsyncSession
        return list(await find_stuck_learning(db, older_than=threshold))


@shared_task(name="app.tasks.sweep.sweep_stuck_learning_documents")
def sweep_stuck_learning_documents(
    stuck_threshold_minutes: int = DEFAULT_STUCK_THRESHOLD_MINUTES,
    max_docs_per_run: int = 100,
) -> dict:
    """Re-enqueue the embedding task for LEARNING docs that look stuck.

    Args:
        stuck_threshold_minutes: A doc is "stuck" if it has been in
            LEARNING since `updated_at < now - threshold`. Must be
            larger than the embedding task's max runtime + retries, or
            the sweep could clobber a primary pipeline that's still
            legitimately running.
        max_docs_per_run: Hard cap on how many docs we touch in one
            sweep tick. Prevents a backlog explosion from turning one
            sweep run into a thundering herd of embedding tasks.

    Returns a small dict for observability. Never raises; sweep tasks
    must be safe to run on a schedule without an operator babysitting.
    """
    threshold = datetime.now(timezone.utc) - timedelta(
        minutes=stuck_threshold_minutes
    )

    try:
        stuck_docs = _run_async(_find_stuck_documents, threshold)
    except Exception:
        # Don't blow up the Beat loop — log and exit cleanly. The next
        # tick will retry.
        logger.exception(
            "sweep_stuck_learning_documents: failed to query stuck docs"
        )
        return {"ok": False, "error": "query_failed"}

    if not stuck_docs:
        return {"ok": True, "found": 0, "requeued": 0}

    # Only touch up to max_docs_per_run. Older leftovers get picked up
    # by subsequent ticks.
    to_process = stuck_docs[:max_docs_per_run]

    requeued = 0
    for doc in to_process:
        try:
            celery_app.send_task(
                "app.tasks.embedding.embed_chunks_for_document",
                args=[str(doc.id), str(doc.owner_id)],
                # No countdown — these are recovery tasks, no transaction
                # visibility race to dodge. Let the worker pick them
                # up as fast as it can.
            )
            requeued += 1
            logger.warning(
                "sweep_stuck_learning_documents: requeued stuck doc "
                "document_id=%s owner_id=%s last_updated=%s",
                doc.id, doc.owner_id, doc.updated_at,
            )
        except Exception:
            # One bad enqueue mustn't kill the rest of the sweep.
            logger.exception(
                "sweep_stuck_learning_documents: failed to enqueue "
                "document_id=%s",
                doc.id,
            )

    return {
        "ok": True,
        "found": len(stuck_docs),
        "requeued": requeued,
        "threshold_minutes": stuck_threshold_minutes,
    }


# ---------------------------------------------------------------------
# Tiny async->sync bridge for the Celery task body
# ---------------------------------------------------------------------
#
# Celery tasks run inside a worker process that does NOT have an
# asyncio event loop running on its thread. The repository's
# `find_stuck_learning` is async (the rest of `document.py` uses async
# sessions for FastAPI compatibility), so we run the coroutine to
# completion with `asyncio.run`. A new loop per call is fine because
# the sweep runs infrequently (every few minutes) and reads at most
# `max_docs_per_run` rows.


def _run_async(coro_factory, *args):
    """Run an async coroutine to completion from sync code.

    Imported lazily so the module stays importable in environments
    that don't run an event loop (e.g. test collection).
    """
    import asyncio

    return asyncio.run(coro_factory(*args))


__all__ = ["sweep_stuck_learning_documents"]
