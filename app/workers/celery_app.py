from celery import Celery
from app.core.config import settings

celery_app = Celery(
    "booppa",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.workers.tasks"],
)

# Celery configuration
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=300,  # 5 minutes
    task_soft_time_limit=250,  # 4 minutes
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    worker_max_tasks_per_child=100,
    # Queue routing
    task_routes={
        "process_report_task": {"queue": "reports"},
        "app.workers.tasks.*": {"queue": "default"},
    },
    # Beat schedule
    beat_schedule={
        "cleanup-old-tasks": {
            "task": "app.workers.tasks.cleanup_old_tasks",
            "schedule": 3600.0,  # Every hour
        },
    },
)
