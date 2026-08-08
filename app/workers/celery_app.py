from celery import Celery
from celery.schedules import crontab
from app.core.config import settings

celery_app = Celery(
    "booppa",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=[
        "app.workers.tasks",
        "app.workers.monthly_credit_reset",
        "app.workers.csp_tasks",
        "app.workers.trm_doc_tasks",
        "app.workers.registry_tasks",
        "app.workers.scout_celery_tasks",
    ],
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
    # A channel error (broker dropped/reset the connection mid-consume) must
    # rebuild the connection rather than leave the consumer parked.
    broker_channel_error_retry=True,
    worker_max_tasks_per_child=100,
    broker_pool_limit=1,
    redis_max_connections=5,
    worker_send_task_events=False,
    # Half-open-socket protection. Redis Cloud dropped the worker's consumer
    # connection during a deploy ("max number of clients reached"); Celery
    # reconnected, logged "ready", then sat blocked in BRPOP for two hours
    # (CLIENT LIST: idle=6772s) while fast_queue/heavy_queue filled up. ECS saw
    # a live process and reported steady state the whole time.
    #
    # health_check_interval is the fix: redis-py PINGs an idle connection on
    # that cadence and rebuilds it if the socket is dead, so a wedged consumer
    # self-heals instead of blocking forever. socket_keepalive lets the OS
    # notice a broken path underneath. Both broker and result backend need it —
    # they are separate connection pools to the same server.
    broker_transport_options={
        "visibility_timeout": 3600,
        "socket_keepalive": True,
        "health_check_interval": 30,
        "retry_on_timeout": True,
        "max_retries": None,
    },
    result_backend_transport_options={
        "visibility_timeout": 3600,
        "socket_keepalive": True,
        "health_check_interval": 30,
        "retry_on_timeout": True,
    },
    # Fallback queue for any task without an explicit route below. MUST be
    # "fast_queue" (a queue the worker consumes via `-Q fast_queue`), not
    # Celery's built-in "celery" queue — otherwise explicitly-named tasks that
    # aren't listed in task_routes (e.g. the per-user first-cycle delivery
    # tasks like send_tender_intelligence_digest_for_user) get enqueued to a
    # queue nothing listens on and silently never run.
    task_default_queue="fast_queue",
    # Queue routing
    task_routes={
        "process_report_task": {"queue": "heavy_queue"},
        "run_deep_scan_task": {"queue": "heavy_queue"},
        "fulfill_notarization_task": {"queue": "heavy_queue"},
        "fulfill_pdpa_task": {"queue": "heavy_queue"},
        "fulfill_vendor_proof_task": {"queue": "heavy_queue"},
        "fulfill_rfp_task": {"queue": "heavy_queue"},
        "fulfill_bundle_task": {"queue": "fast_queue"},
        "fulfill_cover_sheet_task": {"queue": "fast_queue"},
        "vendor_active_health_check_task": {"queue": "fast_queue"},
        "pdpa_monitor_monthly_rescan_task": {"queue": "heavy_queue"},
        "bulk_pdpa_scan_item_task": {"queue": "heavy_queue"},
        "check_compliance_drift_task": {"queue": "fast_queue"},
        # New Heavy Task Routing
        "anchor_scan_ledger_task": {"queue": "heavy_queue"},
        # Bulk DELETEs across sla_logs / webhook_deliveries — table-scan shaped,
        # must not sit in front of latency-sensitive fast_queue work.
        "purge_retention_policies_task": {"queue": "heavy_queue"},
        "scrape_vendor_contact_task": {"queue": "heavy_queue"},
        "run_vendor_pro_pdpa_snapshot_for_user": {"queue": "heavy_queue"},
        "run_pdpa_monitor_report_for_user": {"queue": "heavy_queue"},
        "run_trm_board_report_for_user": {"queue": "heavy_queue"},
        "fulfill_evidence_pack_task": {"queue": "heavy_queue"},
        "anchor_signed_cover_sheet_task": {"queue": "heavy_queue"},
        # Monthly ACRA register refresh — a multi-minute paginated pull.
        "refresh_acra": {"queue": "heavy_queue"},
        # Weekly targeted ACRA pass over known UENs — one request per ~50 UENs,
        # but network-bound and unbounded in the number of UENs we've accumulated.
        "refresh_acra_targeted": {"queue": "heavy_queue"},
        # Weekly PDPC precedent index build — scrapes the decisions register and
        # fetches decision pages for fine/year enrichment (network-heavy).
        "build_pdpc_precedent_index": {"queue": "heavy_queue"},

        "app.workers.tasks.*": {"queue": "fast_queue"},
        # Day-1 CSP baseline: PDF render + S3 upload + ACRA fetch — heavy. Must
        # stay ABOVE the "csp.*" wildcard below; routes match in declaration order.
        "csp.run_baseline": {"queue": "heavy_queue"},
        # CSP Compliance Pack tasks (csp.generate_documents, csp.notarize_record,
        # csp.refresh_sanctions_lists, csp.daily_monitoring, csp.run_sanctions_screening)
        "csp.*": {"queue": "fast_queue"},
        # MAS TRM document pack: seven LLM calls + seven PDF renders + anchoring.
        # heavy_queue, same as the baseline — deliberately NOT a new queue, which
        # would need a worker deployment change and risks enqueueing to a
        # consumer that does not exist.
        "trm.generate_document_pack": {"queue": "heavy_queue"},
    },
    # Beat schedule
    beat_schedule={
        # Weekly reminder that the regulatory citation registry needs a human
        # to re-check it against the regulators' own pages. Detection only —
        # the task never edits an entry. See registry_tasks.py.
        "check-citation-staleness-weekly": {
            "task": "registry.check_citation_staleness",
            "schedule": crontab(day_of_week="monday", hour=1, minute=30),
        },
        # Enforce Enterprise retention policies once a day, off-peak. Daily (not
        # hourly) because retention_days is a day-granularity promise — running
        # more often only adds table scans without deleting anything sooner.
        "purge-retention-policies-daily": {
            "task": "purge_retention_policies_task",
            "schedule": crontab(hour=4, minute=15),
        },
        "cleanup-old-tasks": {
            # Must match the registered task name (name="cleanup_old_tasks");
            # the old dotted "app.workers.tasks.cleanup_old_tasks" is unregistered
            # and was rejected by the worker, so the hourly cleanup never ran.
            "task": "cleanup_old_tasks",
            "schedule": 3600.0,  # Every hour
        },
        # Wedged-worker tripwire — see publish_queue_depth_metrics. Alarm on the
        # metric with treatMissingData=breaching: a wedged worker never runs
        # this task, so the *silence* is itself the alert.
        "publish-queue-depth-metrics": {
            "task": "publish_queue_depth_metrics",
            "schedule": 300.0,  # Every 5 minutes
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
        # Refresh the offline ACRA seed (discovered_vendors) monthly on the 2nd
        # at 04:00 UTC. The register is republished monthly on data.gov.sg;
        # scheduling on the 2nd gives the upstream refresh time to land.
        "refresh-acra-monthly": {
            "task": "refresh_acra",
            "schedule": crontab(day_of_month=2, hour=4, minute=0),
        },
        # Targeted ACRA refresh for UENs already in our own data, weekly on
        # Wednesday 06:00 UTC. The hour offset from the monthly sweep above
        # (2nd @ 04:00) is load-bearing, not cosmetic: the two tasks write the
        # same table with deliberately different ON CONFLICT allowlists, and the
        # Redis lock is per-task-name so it does NOT serialise them against each
        # other. Whenever the 2nd falls on a Wednesday they would otherwise start
        # together; the sweep's multi-minute pull finishes well inside two hours.
        "refresh-acra-targeted-weekly": {
            "task": "refresh_acra_targeted",
            "schedule": crontab(day_of_week="wednesday", hour=6, minute=0),
        },
        # Rebuild the classified PDPC enforcement precedent index weekly (Sunday
        # 04:30 UTC). Feeds the "precedents per finding" feature from live data;
        # cached 14 days so a missed run still serves the previous index.
        "build-pdpc-precedent-index-weekly": {
            "task": "build_pdpc_precedent_index",
            "schedule": crontab(day_of_week="sunday", hour=4, minute=30),
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
        # Daily BID-tender alert: emails subscribers any new BID-rated live
        # tenders closing within the horizon (deduped per vendor) at 01:00 UTC
        # (09:00 SGT) — after the digest cron, before the working day.
        "send-tender-alerts-daily": {
            "task": "send_tender_alerts",
            "schedule": crontab(hour=1, minute=0),
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
        # Vendor Proof: daily expiry sweep at 04:00 UTC — marks lapsed
        # certificates EXPIRED and emails a renewal reminder 30 days out.
        "check-vendor-proof-expiry-daily": {
            "task": "check_vendor_proof_expiry",
            "schedule": crontab(hour=4, minute=0),
        },
        # Trust Passport + Monitor: daily anniversary re-anchor sweep at 04:30 UTC
        # (after monthly PDPA rescan at 03:00 UTC).
        "trust-passport-monitor-reanchor-daily": {
            "task": "run_trust_passport_monitor_reanchors",
            "schedule": crontab(hour=4, minute=30),
        },
        # Vendor Active: daily anniversary cron at 06:00 UTC. Task filters
        # subscribers whose subscription_anniversary_day matches today.
        "vendor-active-daily-anniversary-checks": {
            "task": "run_vendor_active_monthly_checks",
            "schedule": crontab(hour=6, minute=0),
        },
        # Buyer subscriptions: daily anniversary cron at 06:30 UTC. Task filters
        # buyer subscribers whose subscription_anniversary_day matches today and
        # sends the single tiered Procurement Intelligence Digest.
        "buyer-procurement-daily-anniversary-digests": {
            "task": "run_buyer_procurement_monthly_digests",
            "schedule": crontab(hour=6, minute=30),
        },
        # Buyer supplier drift alerts (#1): sweep every watched supplier for a
        # material change (score drop / flip to FLAGGED|CRITICAL / cert expiry)
        # and email the buyer immediately. Every 6 hours; the per-(buyer,supplier)
        # ledger dedups so nothing re-fires. Buyer-side only — vendor flows untouched.
        "buyer-supplier-drift-sweep": {
            "task": "buyer_supplier_drift_sweep_task",
            "schedule": crontab(minute=15, hour="*/6"),
        },
        # Deep-Scan parameter drift: diff each watched supplier's two most recent
        # Deep Scans and alert the buyer when a dimension worsens. Every 6 hours,
        # offset from the status sweep; cache dedup keyed on the current scan_id
        # ensures a given new scan alerts each buyer at most once.
        "buyer-deep-scan-drift-sweep": {
            "task": "buyer_deep_scan_drift_sweep_task",
            "schedule": crontab(minute=45, hour="*/6"),
        },
        # Standard/Pro Suite: monthly MAS TRM board report on the 1st at 05:00 UTC.
        "trm-monthly-board-reports": {
            "task": "run_trm_monthly_board_reports",
            "schedule": crontab(day_of_month=1, hour=5, minute=0),
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
        # CSP Compliance Pack — refresh OFAC/UN sanctions caches at 22:00 UTC (06:00 SGT),
        # before the daily monitoring scan at 23:00 UTC (07:00 SGT).
        "csp-refresh-sanctions-daily": {
            "task": "csp.refresh_sanctions_lists",
            "schedule": crontab(hour=22, minute=0),
        },
        "csp-daily-monitoring": {
            "task": "csp.daily_monitoring",
            "schedule": crontab(hour=23, minute=0),
        },
        # SCOUT Agents: scheduled scoring, send batching, weekly digest
        "scout-vendor-scoring-weekly": {
            "task": "scout_vendor_scoring_task",
            "schedule": crontab(day_of_week=1, hour=4, minute=0),
            "options": {"queue": "heavy_queue"},
        },
        "scout-buyer-scoring-weekly": {
            "task": "scout_buyer_scoring_task",
            "schedule": crontab(day_of_week=1, hour=4, minute=30),
            "options": {"queue": "heavy_queue"},
        },
        # PAUSED 2026-08-07 — the outreach this task sent breached the Spam
        # Control Act (Cap. 311A) Second Schedule. See AUDIT_2026-08-07.md P0-3.
        #
        # The code defects are FIXED: "<ADV>" subject prefix, in-body
        # unsubscribe link, sender postal address, no "[Name]" placeholder, and
        # no asserted gaps the scan did not detect. A send-side gate
        # (scout_celery_tasks._outreach_compliance_error) now blocks any message
        # missing those, including bodies stored before the fix.
        #
        # Still blocked on ONE operational step, which is why this stays
        # commented out: COMPANY_POSTAL_ADDRESS must be set to Booppa's real
        # registered address. It has no default and was deliberately not
        # guessed — a wrong address satisfies nothing. With it unset the task
        # aborts immediately, so re-enabling now would simply run a daily no-op.
        #
        # To resume: set COMPANY_POSTAL_ADDRESS, regenerate the stored bodies of
        # any rows still in APPROVED (they predate the fix and the gate will
        # refuse them), inspect one real rendered send by eye, then uncomment.
        # Scoring/approval keep running; prospects accrue in APPROVED unsent.
        # "scout-send-approved-daily": {
        #     "task": "scout_send_approved_batch_task",
        #     "schedule": crontab(hour=9, minute=0),
        #     "options": {"queue": "heavy_queue"},
        # },
        "scout-weekly-digest": {
            "task": "scout_weekly_digest_task",
            "schedule": crontab(day_of_week=1, hour=8, minute=0),
            "options": {"queue": "heavy_queue"},
        },
    },
)

# Redis connection exhaustion on the free tier (30-client ceiling): gossip,
# mingle and the heartbeat must stay OFF. They are disabled by the CLI flags
# `--without-gossip --without-mingle --without-heartbeat` on every worker
# command line (.github/workflows/ci.yml and docker-compose.yml) — that is the
# only mechanism that works. There is no `worker_enable_gossip`/`_mingle`
# setting to configure here, and discarding the bootstep classes from
# `celery_app.steps['consumer']` does NOT work either: that set holds only
# *custom* steps, while Mingle/Gossip/Heart live in the Consumer blueprint's own
# default_steps. A previous version of this file did exactly that and the
# bootsteps kept running.
#
# Why it matters: Gossip opens an extra broker channel beside the consumer's.
# When that channel raises (e.g. "max number of clients reached" during a
# deploy) the whole consumer blueprint restarts, opening fresh connections that
# fail the same way — one dropped connection becomes a ~1/second reconnect storm
# that keeps Redis saturated and locks you out of inspecting it.


# ── Live pull of reference datasets on deploy ────────────────────────────────
# On worker boot (which happens on every deploy, since the worker container
# restarts) enqueue a one-shot bootstrap that pulls the ACRA seed and PDPC
# precedent index if they're missing/stale. Without this a fresh deploy would
# wait for the monthly/weekly Beat tick before those datasets populate.
# `bootstrap_reference_data` self-gates on freshness and holds a Redis debounce,
# so this is cheap and safe to fire on every worker start / across replicas.
from celery.signals import worker_ready


@worker_ready.connect
def _pull_reference_data_on_boot(**_kwargs):  # pragma: no cover - boot-time hook
    try:
        celery_app.send_task("bootstrap_reference_data")
    except Exception as exc:
        # A broker hiccup at boot must never stop the worker from coming up.
        import logging
        logging.getLogger(__name__).warning(
            "[Bootstrap] failed to enqueue reference-data pull on boot: %s", exc
        )
