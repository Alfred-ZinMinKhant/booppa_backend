"""
Auto-Activation Worker
======================
Periodic task that checks growth metrics and auto-activates feature flags.
Runs every hour via Celery beat.
"""

import logging
from app.workers.celery_app import celery_app
from app.core.db import SessionLocal

logger = logging.getLogger(__name__)


@celery_app.task(name="auto_activation_check")
def auto_activation_check():
    """Check growth metrics and auto-activate feature flags."""
    from app.services.feature_flags import check_auto_activation, get_growth_metrics

    db = SessionLocal()
    try:
        metrics = get_growth_metrics(db)
        logger.info(f"Auto-activation check — metrics: {metrics}")

        activated = check_auto_activation(db)
        if activated:
            logger.info(f"Auto-activated flags: {activated}")
        else:
            logger.info("No flags auto-activated this cycle")

        return {"metrics": metrics, "activated": activated}
    except Exception as e:
        logger.error(f"Auto-activation check failed: {e}")
        return {"error": str(e)}
    finally:
        db.close()


@celery_app.task(name="compute_quarterly_leaderboard")
def compute_quarterly_leaderboard_task(quarter: str = None):
    """Compute quarterly leaderboard (triggered by scheduler or admin)."""
    from app.services.leaderboard import compute_quarterly_leaderboard

    db = SessionLocal()
    try:
        result = compute_quarterly_leaderboard(db, quarter=quarter)
        logger.info(f"Leaderboard computed: {result}")
        return result
    except Exception as e:
        logger.error(f"Leaderboard computation failed: {e}")
        return {"error": str(e)}
    finally:
        db.close()


@celery_app.task(name="compute_monthly_snapshot")
def compute_monthly_snapshot_task(month: str = None):
    """Compute monthly subscription snapshot."""
    from app.services.funnel_analytics import compute_monthly_snapshot
    from datetime import datetime, timezone

    if not month:
        now = datetime.now(timezone.utc)
        month = f"{now.year}-{now.month:02d}"

    db = SessionLocal()
    try:
        snapshot = compute_monthly_snapshot(db, month)
        logger.info(f"Monthly snapshot computed for {month}: MRR={snapshot.total_mrr_cents}")
        return {"month": month, "mrr": snapshot.total_mrr_cents}
    except Exception as e:
        logger.error(f"Monthly snapshot failed: {e}")
        return {"error": str(e)}
    finally:
        db.close()
