from celery import Celery
from celery.schedules import crontab
from app.core.config import settings

celery_app = Celery(
    "booppa",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.workers.tasks", "app.workers.monthly_credit_reset"],
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
    # Fallback queue for any task without an explicit route below. MUST be
    # "default" (a queue the worker consumes via `-Q reports,default`), not
    # Celery's built-in "celery" queue — otherwise explicitly-named tasks that
    # aren't listed in task_routes (e.g. the per-user first-cycle delivery
    # tasks like send_tender_intelligence_digest_for_user) get enqueued to a
    # queue nothing listens on and silently never run.
    task_default_queue="default",
    # Queue routing
    task_routes={
        "process_report_task": {"queue": "reports"},
        "fulfill_notarization_task": {"queue": "reports"},
        "fulfill_pdpa_task": {"queue": "reports"},
        "fulfill_vendor_proof_task": {"queue": "reports"},
        "fulfill_rfp_task": {"queue": "reports"},
        "fulfill_bundle_task": {"queue": "default"},
        "fulfill_cover_sheet_task": {"queue": "default"},
        "vendor_active_health_check_task": {"queue": "default"},
        "pdpa_monitor_monthly_rescan_task": {"queue": "reports"},
        "check_compliance_drift_task": {"queue": "default"},
        "app.workers.tasks.*": {"queue": "default"},
    },
    # Beat schedule
    beat_schedule={
        "cleanup-old-tasks": {
            # Must match the registered task name (name="cleanup_old_tasks");
            # the old dotted "app.workers.tasks.cleanup_old_tasks" is unregistered
            # and was rejected by the worker, so the hourly cleanup never ran.
            "task": "cleanup_old_tasks",
            "schedule": 3600.0,  # Every hour
        },
        # Backstop for Compliance Evidence Pack cover-sheet delivery — re-fires
        # the idempotent _maybe_fire_cover_sheet for any buyer still owed a
        # cover sheet, so a missed inline trigger self-heals within the hour
        # instead of silently never delivering the 3rd bundle document.
        "sweep-pending-cover-sheets": {
            "task": "sweep_pending_cover_sheets",
            "schedule": 3600.0,  # Every hour
        },
        # Auto-recover signed cover-sheet blockchain anchors stuck in "Pending"
        # after their inline retries were exhausted (transient RPC/gas outages),
        # bounded so a genuinely un-anchorable hash eventually stops retrying.
        "retry-failed-cover-sheet-anchors": {
            "task": "retry_failed_cover_sheet_anchors",
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
        # Tender Intelligence subscribers: daily run at 00:00 UTC (08:00 SGT).
        # The task itself filters to subscribers whose subscription_anniversary_day
        # matches today's day-of-month, so each subscriber gets the digest on
        # the same day they subscribed each month.
        "send-tender-intelligence-digest-daily-anniversary": {
            "task": "send_tender_intelligence_digest",
            "schedule": crontab(hour=0, minute=0),
        },
        # Vendor Pro subscribers: daily competitor-activity digest at 00:00 UTC
        # (08:00 SGT). Summarises last 24h of TenderCheckLookup activity on
        # tenders each subscriber has tracked.
        "send-vendor-pro-daily-alerts": {
            "task": "send_vendor_pro_daily_alerts",
            "schedule": crontab(hour=0, minute=0),
        },
        # Vendor Pro: monthly Competitor Awareness Signals. Fires daily at
        # 08:30 UTC; the task filters to subscribers whose anniversary day
        # matches today (same per-subscriber anniversary pattern as the other
        # monthly deliverables), so each subscriber receives it once a month.
        "send-vendor-pro-competitor-signals-monthly": {
            "task": "send_vendor_pro_monthly_competitor_signals",
            "schedule": crontab(day_of_month="*", hour=8, minute=30),
        },
        # Vendor Pro subscribers: quarterly PDPA rescans on the 1st of
        # Jan/Apr/Jul/Oct at 03:30 UTC (30 minutes after PDPA Monitor's
        # monthly rescan window so the worker queue doesn't double-spike).
        "vendor-pro-quarterly-pdpa-rescans": {
            "task": "run_vendor_pro_quarterly_pdpa_rescans",
            "schedule": crontab(day_of_month=1, month_of_year="1,4,7,10", hour=3, minute=30),
        },
        # Send every active vendor their weekly score digest every Monday at 08:00 UTC.
        "send-weekly-vendor-scores": {
            "task": "send_weekly_vendor_scores",
            "schedule": crontab(day_of_week="monday", hour=8, minute=0),
        },
        # Vendor Active: daily anniversary cron at 06:00 UTC. Task filters
        # subscribers whose subscription_anniversary_day matches today.
        "vendor-active-daily-anniversary-checks": {
            "task": "run_vendor_active_monthly_checks",
            "schedule": crontab(hour=6, minute=0),
        },
        # Pre-seed notarization credit rows for active suite/enterprise subscribers
        # on the 1st of each month at 00:30 UTC. Lazy creation in notarize.py
        # remains the source of truth — this just makes allocations visible early.
        "reset-monthly-notarization-credits": {
            "task": "reset_monthly_notarization_credits",
            "schedule": crontab(day_of_month=1, hour=0, minute=30),
        },
        # PDPA Monitor: daily anniversary cron at 03:00 UTC.
        "pdpa-monitor-daily-anniversary-rescans": {
            "task": "run_pdpa_monitor_monthly_rescans",
            "schedule": crontab(hour=3, minute=0),
        },
        # Compliance Evidence Monthly: daily anniversary cron at 04:00 UTC.
        "compliance-evidence-daily-anniversary-refresh": {
            "task": "run_compliance_evidence_monthly_refresh",
            "schedule": crontab(hour=4, minute=0),
        },
        # Compliance Evidence Monthly: intake-confirmation nudge fires ~6 days
        # before each subscriber's anniversary (task internally computes
        # target_day = today + 6). Daily 02:00 UTC.
        "compliance-evidence-daily-anniversary-intake-nudge": {
            "task": "send_monthly_intake_refresh_task",
            "schedule": crontab(hour=2, minute=0),
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
