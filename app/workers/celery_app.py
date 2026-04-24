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
    task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True,
    worker_max_tasks_per_child=100,
    # Queue routing
    task_routes={
        "process_report_task": {"queue": "reports"},
        "fulfill_notarization_task": {"queue": "reports"},
        "fulfill_pdpa_task": {"queue": "reports"},
        "fulfill_vendor_proof_task": {"queue": "reports"},
        "fulfill_rfp_task": {"queue": "reports"},
        "vendor_active_health_check_task": {"queue": "default"},
        "pdpa_monitor_quarterly_rescan_task": {"queue": "reports"},
        "app.workers.tasks.*": {"queue": "default"},
    },
    # Beat schedule
    beat_schedule={
        "cleanup-old-tasks": {
            "task": "app.workers.tasks.cleanup_old_tasks",
            "schedule": 3600.0,  # Every hour
        },
        # Sync live GeBIZ open tenders every 30 minutes.
        "sync-gebiz-tenders": {
            "task": "sync_gebiz_tenders",
            "schedule": 1800.0,  # 30 minutes
        },
        # Refresh GeBIZ tender base rates from data.gov.sg every Monday at 02:00 UTC.
        # Uses real procurement award data to calibrate win probability calculations.
        # Task registered as name="refresh_gebiz_base_rates" in tasks.py.
        "refresh-gebiz-base-rates-weekly": {
            "task": "refresh_gebiz_base_rates",
            "schedule": crontab(day_of_week="monday", hour=2, minute=0),
        },
        # Send every active vendor curated GeBIZ tender alerts every Monday at 07:00 UTC.
        # Runs one hour before the score digest so vendors open the score email in context.
        "send-gebiz-alert-newsletter": {
            "task": "send_gebiz_alert_newsletter",
            "schedule": crontab(day_of_week="monday", hour=7, minute=0),
        },
        # Send every active vendor their weekly score digest every Monday at 08:00 UTC.
        "send-weekly-vendor-scores": {
            "task": "send_weekly_vendor_scores",
            "schedule": crontab(day_of_week="monday", hour=8, minute=0),
        },
        # Vendor Active: run monthly profile health checks on the 1st of each month at 06:00 UTC.
        "vendor-active-monthly-health-checks": {
            "task": "run_vendor_active_monthly_checks",
            "schedule": crontab(day_of_month=1, hour=6, minute=0),
        },
        # PDPA Monitor: quarterly re-scans on the 1st of Jan, Apr, Jul, Oct at 03:00 UTC.
        "pdpa-monitor-quarterly-rescans": {
            "task": "run_pdpa_monitor_quarterly_rescans",
            "schedule": crontab(month_of_year="1,4,7,10", day_of_month=1, hour=3, minute=0),
        },
        # Weekly intelligence brief — all vendors with completed reports.
        # Monday 00:00 UTC = 08:00 SGT, fires before the score digest.
        "weekly-intelligence-brief": {
            "task": "weekly_intelligence_brief",
            "schedule": crontab(day_of_week="monday", hour=0, minute=0),
        },
        # Recompute sector percentiles for all vendors every Sunday at 23:00 UTC.
        # Runs just before the Monday score digest so government portal shows fresh ranks.
        "recompute-all-vendor-percentiles": {
            "task": "recompute_all_vendor_percentiles",
            "schedule": crontab(day_of_week="sunday", hour=23, minute=0),
        },
        # Scrape contact emails from MarketplaceVendor websites nightly at 03:00 UTC (11:00 SGT).
        # Only targets vendors that have a domain/website but no contact_email yet.
        # Staggered 5s per vendor internally — safe for rate limits.
        "scrape-marketplace-vendor-contacts-nightly": {
            "task": "scrape_vendor_contacts_batch",
            "schedule": crontab(hour=3, minute=0),
            "kwargs": {"model": "marketplace", "limit": 100},
        },
        # Scrape discovered vendors (GeBIZ/ACRA) nightly at 03:30 UTC.
        # Offset by 30 minutes to avoid overlap with marketplace scrape.
        "scrape-discovered-vendor-contacts-nightly": {
            "task": "scrape_vendor_contacts_batch",
            "schedule": crontab(hour=3, minute=30),
            "kwargs": {"model": "discovered", "limit": 100},
        },
    },
)
