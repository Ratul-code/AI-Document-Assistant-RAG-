#   """Celery application instance.

#   The Celery app is configured from app.core.config (pydantic-settings),
#   so broker/backend URLs come from .env (CELERY_BROKER_URL / CELERY_RESULT_BACKEND).
#   """

from celery import Celery

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "ai_doc_analyser",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_track_started=True,          # mark tasks as STARTED (useful for monitoring)
    task_time_limit= 15*  60,              # hard kill after 15 min
    task_soft_time_limit= 10 * 60,         # raise SoftTimeLimitExceeded after 10 min
    worker_prefetch_multiplier=1,     # fair dispatch: don't hoard tasks
    timezone="UTC",
    enable_utc=True,
    accept_content=["json"],
    task_serializer="json",
    result_serializer="json",
    # Beat schedule — run by `celery -A ... beat` (separate process).
    # Periodic tasks live here so a fresh deployment only needs to
    # start one Beat process to pick them up; the workers themselves
    # just consume what Beat enqueues.
    beat_schedule={
        # Every 5 minutes: re-enqueue the embedding task for any docs
        # that have been stuck in LEARNING. See
        # app.tasks.sweep.sweep_stuck_learning_documents for the
        # rationale (process_document / embedding worker crash
        # recovery; broker drop-recovery).
        "sweep-stuck-learning-documents": {
            "task": "app.tasks.sweep.sweep_stuck_learning_documents",
            # 5 minutes: balances recovery latency against wasted scans
            # when there's nothing to do. The task is cheap (one
            # indexed SELECT + zero or more send_task calls).
            "schedule": 300.0,
        },
    },
)

# Auto-discover @shared_task definitions under app.tasks.*
celery_app.autodiscover_tasks(["app.tasks"])