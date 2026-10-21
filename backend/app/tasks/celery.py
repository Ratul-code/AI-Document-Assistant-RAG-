"""Celery worker entry point.

Run a worker with:
    celery -A app.tasks.celery worker --loglevel=info

Importing `app.tasks` here triggers `app.tasks/__init__.py`, which in turn
imports each task module (e.g. `app.tasks.document`) so its `@shared_task`
functions register on the configured Celery app.
"""

from app.core.celery_app import celery_app
from app.tasks import document  # noqa: F401  -- ensures @shared_task registration
from app.tasks import embedding  # noqa: F401  -- registers embed_chunks_for_document

__all__ = ("celery_app",)
