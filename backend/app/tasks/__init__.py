"""Celery tasks package.

Importing the task modules here ensures Celery's autodiscover() picks them up.
`@shared_task` registers each task with the Celery app on import.
"""

from app.tasks import (
    document,  # noqa: F401
    embedding,  # noqa: F401
    sweep,  # noqa: F401
)
