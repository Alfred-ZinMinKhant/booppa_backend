from celery import Celery
from celery.schedules import crontab
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
        "fulfill_notarization_task": {"queue": "reports"},
        "fulfill_rfp_task": {"queue": "reports"},
        "app.workers.tasks.*": {"queue": "default"},
    },
    # Beat schedule
    beat_schedule={
        "cleanup-old-tasks": {
            "task": "app.workers.tasks.cleanup_old_tasks",
            "schedule": 3600.0,  # Every hour
        },
        # Refresh GeBIZ tender base rates from data.gov.sg every Monday at 02:00 UTC.
        # Uses real procurement award data to calibrate win probability calculations.
        # Task registered as name="refresh_gebiz_base_rates" in tasks.py.
        "refresh-gebiz-base-rates-weekly": {
            "task": "refresh_gebiz_base_rates",
            "schedule": crontab(day_of_week="monday", hour=2, minute=0),
        },
    },
)
