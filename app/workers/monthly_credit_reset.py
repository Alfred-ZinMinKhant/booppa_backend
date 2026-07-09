"""
Monthly notarization credit pre-seeding for suite/enterprise subscribers.

The lazy logic in app/api/notarize.py already provides correct per-month
behavior — `used` is keyed by (user_id, month), so a new month naturally
starts at 0/limit. This task pre-creates the row on the 1st of each month
for every active suite/enterprise subscriber so dashboards show the
allocation before the first notarization is consumed.
"""

from datetime import datetime, timezone

from .celery_app import celery_app
from app.core.db import SessionLocal


@celery_app.task(name="reset_monthly_notarization_credits")
def reset_monthly_notarization_credits():
    from app.core.models import User
    from app.core.models import NotarizationCredit, ENTERPRISE_NOTARIZATION_LIMITS

    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    eligible_plans = set(ENTERPRISE_NOTARIZATION_LIMITS.keys())

    db = SessionLocal()
    created = 0
    skipped = 0
    try:
        users = (
            db.query(User)
            .filter(User.plan.in_(eligible_plans))
            .all()
        )
        for user in users:
            limit = ENTERPRISE_NOTARIZATION_LIMITS.get(user.plan)
            if limit is None:
                continue

            existing = (
                db.query(NotarizationCredit)
                .filter(
                    NotarizationCredit.user_id == user.id,
                    NotarizationCredit.month == current_month,
                )
                .first()
            )
            if existing:
                skipped += 1
                continue

            db.add(
                NotarizationCredit(
                    user_id=user.id,
                    month=current_month,
                    used=0,
                    monthly_limit=limit,
                )
            )
            created += 1

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    return {"month": current_month, "created": created, "skipped": skipped}
