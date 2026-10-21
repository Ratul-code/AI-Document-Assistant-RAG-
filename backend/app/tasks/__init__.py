"""Celery tasks package.

Importing the task modules here ensures Celery's autodiscover() picks them up.
`@shared_task` registers each task with the Celery app on import.
"""

from app.tasks import document  # noqa: F401
from app.tasks import embedding  # noqa: F401
from app.tasks import sweep  # noqa: F401
