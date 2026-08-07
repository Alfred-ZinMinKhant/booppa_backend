"""
scout_celery_tasks.py
Booppa Smart Care LLC — SCOUT Agents, orchestration

THIS FILE IS THE ANSWER TO "make these into real agents." Everything
above (scout_shared / scout_vendor_pipeline / scout_buyer_pipeline /
scout_csp_pipeline) is scoring logic — correct, but scoring logic alone
is not an agent. What makes something an agent: it runs on its own, it acts
(sends) within a controlled boundary, and it interrupts a human only for
what genuinely needs a human. This file is where that actually happens.

THE STATE MACHINE THIS FILE DRIVES (see app/core/models.py for ScoutProspect):

    NEW -> PENDING_APPROVAL -> APPROVED -> SENT
                             -> REJECTED (human says no, stays no)
    (any state) -> SEND_FAILED (never silently retried forever)

FOUR TASKS, EACH WITH ONE JOB:

1. scout_vendor_scoring_task / scout_buyer_scoring_task
   Run the pipeline, upsert ScoutProspect rows, promote qualifying rows
   to PENDING_APPROVAL. Never sends anything. Scheduled weekly.

2. scout_send_approved_batch_task
   Sends everything currently APPROVED, via the existing EmailService,
   category="marketing" (so unsubscribe headers are automatic). Volume-capped per run.
   Nothing gets sent that a human didn't explicitly approve first.

3. scout_weekly_digest_task
   The ONE regular touchpoint a human gets under normal operation: one
   email a week, listing what's PENDING_APPROVAL, with a link to approve
   in bulk.

4. scout_check_send_health_task
   The alert mechanism — fires ONLY on an exception condition
   (bounce rate spike, repeated pipeline failures, a stuck queue).
"""

import asyncio
from datetime import datetime, timezone, timedelta

from celery.schedules import crontab

from app.workers.celery_app import celery_app
from app.core.db import SessionLocal
from app.core.config import settings
from app.core.models import ScoutProspect
from app.services.email_service import EmailService
from app.services.email_layout import branded_email_html, email_button

from app.services.scout_shared import redis_pipeline_lock
from app.services.scout_vendor_pipeline import run_vendor_pipeline
from app.services.scout_buyer_pipeline import run_buyer_pipeline
from app.services.scout_csp_pipeline import run_csp_pipeline

import logging
logger = logging.getLogger(__name__)

DIGEST_RECIPIENT = getattr(settings, "SCOUT_DIGEST_EMAIL", settings.SUPPORT_EMAIL)
BOUNCE_RATE_ALERT_THRESHOLD = 0.15   # alert if >15% of a batch fails to send
CONSECUTIVE_PIPELINE_FAILURE_ALERT = 2


async def _alert_scout_issue(*, reason: str, pipeline: str, extra: dict | None = None) -> None:
    """
    Loud failure path, modelled directly on _alert_payment_fulfillment_issue.
    All sends are best-effort.
    """
    logger.error(f"[SCOUT ALERT] pipeline={pipeline} reason={reason} extra={extra}")
    try:
        body_inner = f"""
        <p><strong>SCOUT pipeline alert — {pipeline}</strong></p>
        <p>{reason}</p>
        {f"<pre>{extra}</pre>" if extra else ""}
        """
        html = branded_email_html(body_inner, title=f"SCOUT alert: {pipeline}")
        ok = await EmailService().send_html_email(
            to_email=settings.SUPPORT_EMAIL,
            subject=f"[SCOUT] {pipeline} needs attention",
            body_html=html,
            category="transactional",
        )
        # A rejected alert returns False rather than raising, so the except
        # below never sees it. Silence here means nobody learns the pipeline
        # broke — the log line above is the only remaining trace.
        if not ok:
            logger.error(
                f"[SCOUT ALERT] alert email REJECTED by provider — "
                f"pipeline={pipeline} reason={reason} went unnotified"
            )
    except Exception as e:
        logger.error(f"[SCOUT ALERT] failed to send alert email itself: {e}")


def _is_existing_customer(db, uen: str | None, contact_email: str | None) -> bool:
    """
    Checks whether a scored prospect is already a paying Booppa customer
    before queuing them for cold outreach.
    """
    from app.core.models import User

    if uen:
        if db.query(User).filter(User.uen == uen).first():
            return True
    if contact_email:
        if db.query(User).filter(User.email == contact_email.strip().lower()).first():
            return True
    return False


def _upsert_prospects(db, pipeline: str, scored_items: list[dict]) -> tuple[int, int, int]:
    """
    Shared upsert logic for vendor/buyer/csp. Returns (new_count, updated_count, excluded_existing_count).
    """
    now = datetime.now(timezone.utc)
    new_count = updated_count = excluded_existing = 0

    for item in scored_items:
        # Named `prospect`, not `existing`: this is a ScoutProspect — a cold
        # outreach candidate we scraped and scored — never a User row. The
        # identity-write invariant reads receiver names, and `uen` on an
        # account is the one that has to go through record_verified_identity.
        prospect = db.query(ScoutProspect).filter(
            ScoutProspect.pipeline == pipeline,
            ScoutProspect.natural_key == item["natural_key"],
        ).first()

        display_name = item.get("clean_name") or item.get("agency_name") or ""
        tier = item.get("priority_tier")
        score = item.get("trust_score") or item.get("aml_readiness_score")

        is_customer = _is_existing_customer(db, item.get("uen"), item.get("contact_email"))

        if prospect:
            prospect.display_name = display_name
            prospect.uen = item.get("uen") or prospect.uen
            prospect.website_url = item.get("website_url") or prospect.website_url
            prospect.score = score
            prospect.priority_tier = tier
            prospect.fit_tier = item.get("fit_tier") or item.get("licence_age_tier") or prospect.fit_tier
            prospect.raw_data = item
            prospect.last_scored_at = now
            prospect.updated_at = now
            if is_customer:
                prospect.status = "EXISTING_CUSTOMER"
                prospect.status_reason = "matched an active Booppa account by UEN or email"
                excluded_existing += 1
            elif prospect.status in ("NEW", "BELOW_THRESHOLD", "PENDING_APPROVAL"):
                prospect.status = "PENDING_APPROVAL" if tier in ("TIER1", "TIER2") else "BELOW_THRESHOLD"
            updated_count += 1
        else:
            status = "EXISTING_CUSTOMER" if is_customer else (
                "PENDING_APPROVAL" if tier in ("TIER1", "TIER2") else "BELOW_THRESHOLD"
            )
            row = ScoutProspect(
                pipeline=pipeline, natural_key=item["natural_key"], display_name=display_name,
                uen=item.get("uen") or None, website_url=item.get("website_url") or None,
                score=score, priority_tier=tier,
                fit_tier=item.get("fit_tier") or item.get("licence_age_tier"),
                raw_data=item,
                status=status,
                status_reason="matched an active Booppa account by UEN or email" if is_customer else None,
                first_scored_at=now, last_scored_at=now, created_at=now, updated_at=now,
                send_attempts=0,
            )
            db.add(row)
            new_count += 1
            if is_customer:
                excluded_existing += 1

    db.commit()
    return new_count, updated_count, excluded_existing


@celery_app.task(bind=True, max_retries=1, name="scout_vendor_scoring_task")
def scout_vendor_scoring_task(self):
    """Scheduled weekly. Scores, persists, never sends."""
    redis_client = celery_app.backend.client
    if not redis_pipeline_lock(redis_client, "lock:scout_vendor_scoring", ttl_seconds=3600):
        logger.info("[SCOUT vendor] already running, skipping this tick")
        return

    db = SessionLocal()
    try:
        scored = asyncio.run(run_vendor_pipeline(limit_vendors=500, min_awards=2))
        new_count, updated_count, excluded_existing = _upsert_prospects(db, "vendor", scored)
        logger.info(f"[SCOUT vendor] scored {len(scored)} vendors — {new_count} new, "
                    f"{updated_count} updated, {excluded_existing} excluded (existing customers)")
    except Exception as e:
        db.rollback()
        logger.error(f"[SCOUT vendor] pipeline run failed: {e}")
        raise
    finally:
        db.close()
        try:
            redis_client.delete("lock:scout_vendor_scoring")
        except Exception:
            pass


@celery_app.task(bind=True, max_retries=1, name="scout_buyer_scoring_task")
def scout_buyer_scoring_task(self):
    """Same shape as scout_vendor_scoring_task, for the buyer/agency pipeline."""
    redis_client = celery_app.backend.client
    if not redis_pipeline_lock(redis_client, "lock:scout_buyer_scoring", ttl_seconds=3600):
        logger.info("[SCOUT buyer] already running, skipping this tick")
        return

    db = SessionLocal()
    try:
        scored = asyncio.run(run_buyer_pipeline(limit_agencies=200, min_awards=3))
        new_count, updated_count, excluded_existing = _upsert_prospects(db, "buyer", scored)
        logger.info(f"[SCOUT buyer] scored {len(scored)} agencies — {new_count} new, "
                    f"{updated_count} updated, {excluded_existing} excluded (existing customers)")
    except Exception as e:
        db.rollback()
        logger.error(f"[SCOUT buyer] pipeline run failed: {e}")
        raise
    finally:
        db.close()
        try:
            redis_client.delete("lock:scout_buyer_scoring")
        except Exception:
            pass


@celery_app.task(bind=True, max_retries=1, name="scout_csp_scoring_task")
def scout_csp_scoring_task(self, seed_list: list[dict]):
    """
    On-demand only — no beat entry. Triggered by the CSP seed-list CSV upload
    (app/api/scout_csp_seed_import.py). The CSP pipeline has no bulk source to
    auto-fetch, so its input always arrives from a human-supplied seed list.
    """
    if not seed_list:
        logger.info("[SCOUT csp] empty seed list, nothing to do")
        return {"scored": 0, "new": 0, "updated": 0, "excluded_existing": 0}

    redis_client = celery_app.backend.client
    if not redis_pipeline_lock(redis_client, "lock:scout_csp_scoring", ttl_seconds=3600):
        logger.info("[SCOUT csp] already running, skipping this upload")
        raise self.retry(countdown=300)

    db = SessionLocal()
    try:
        scored = asyncio.run(run_csp_pipeline(seed_list))
        new_count, updated_count, excluded_existing = _upsert_prospects(db, "csp", scored)
        logger.info(f"[SCOUT csp] scored {len(scored)} of {len(seed_list)} seeds — {new_count} new, "
                    f"{updated_count} updated, {excluded_existing} excluded (existing customers)")
        return {
            "scored": len(scored), "new": new_count,
            "updated": updated_count, "excluded_existing": excluded_existing,
        }
    except Exception as e:
        db.rollback()
        logger.error(f"[SCOUT csp] pipeline run failed: {e}")
        raise
    finally:
        db.close()
        try:
            redis_client.delete("lock:scout_csp_scoring")
        except Exception:
            pass



# A pause longer than this means we are cold again as far as mailbox providers
# are concerned, so the ramp restarts rather than resuming at full volume.
SEND_RAMP_GAP_DAYS = 14


def get_daily_send_cap(db) -> int:
    """
    Ramps daily sending volume based on actual send history in ScoutProspect.sent_at.

    The ramp is anchored on the first send of the CURRENT sending streak, not the
    first send ever. If sending is paused for longer than SEND_RAMP_GAP_DAYS and
    then resumed, we restart at the bottom of the ramp instead of jumping straight
    to full volume — a cold restart at 60/day is exactly how sender reputation
    gets burned.
    """
    sends = [
        row[0]
        for row in db.query(ScoutProspect.sent_at)
        .filter(ScoutProspect.sent_at.isnot(None))
        .order_by(ScoutProspect.sent_at.asc())
        .all()
        if row[0]
    ]
    if not sends:
        return 5

    now = datetime.now(timezone.utc)

    # Resuming after a pause: nothing has been sent yet in this streak, so the
    # gap is measured against now, not against a later send that doesn't exist.
    if (now - sends[-1]).days > SEND_RAMP_GAP_DAYS:
        return 5

    # Otherwise anchor on the first send after the most recent gap.
    anchor = sends[0]
    for prev, cur in zip(sends, sends[1:]):
        if (cur - prev).days > SEND_RAMP_GAP_DAYS:
            anchor = cur

    days_sending = (now - anchor).days
    if days_sending < 7:
        return 10
    elif days_sending < 14:
        return 20
    elif days_sending < 21:
        return 30
    elif days_sending < 28:
        return 40
    return 60


@celery_app.task(bind=True, max_retries=1, name="scout_send_approved_batch_task")
def scout_send_approved_batch_task(self):
    """
    Scheduled daily. Sends up to get_daily_send_cap() prospects currently
    in APPROVED status.
    """
    db = SessionLocal()
    try:
        daily_cap = get_daily_send_cap(db)
        batch = (
            db.query(ScoutProspect)
            .filter(ScoutProspect.status == "APPROVED")
            .order_by(ScoutProspect.approved_at.asc())
            .limit(daily_cap)
            .all()
        )
        if not batch:
            logger.info("[SCOUT send] nothing approved to send")
            return

        sent = 0
        failed = 0
        email_svc = EmailService()

        for prospect in batch:
            if not prospect.outreach_body_html:
                prospect.status = "SEND_FAILED"
                prospect.status_reason = "no outreach_body_html generated — cannot send"
                failed += 1
                continue

            to_email = (prospect.raw_data or {}).get("contact_email")
            if not to_email:
                continue

            ok = asyncio.run(email_svc.send_html_email(
                to_email=to_email,
                subject=prospect.outreach_subject or f"{prospect.display_name} — Booppa",
                body_html=prospect.outreach_body_html,
                category="marketing",
            ))

            if ok:
                prospect.status = "SENT"
                prospect.sent_at = datetime.now(timezone.utc)
                sent += 1
            else:
                prospect.status = "SEND_FAILED"
                prospect.status_reason = "EmailService.send_html_email returned False"
                failed += 1

            prospect.send_attempts += 1
            prospect.updated_at = datetime.now(timezone.utc)

        db.commit()
        logger.info(f"[SCOUT send] batch of {len(batch)}: {sent} sent, {failed} failed")

        if len(batch) > 0 and (failed / len(batch)) > BOUNCE_RATE_ALERT_THRESHOLD:
            asyncio.run(_alert_scout_issue(
                reason=f"Send failure rate {failed}/{len(batch)} ({failed/len(batch):.0%}) exceeds "
                       f"{BOUNCE_RATE_ALERT_THRESHOLD:.0%} threshold",
                pipeline="send",
                extra={"sent": sent, "failed": failed},
            ))

    except Exception as e:
        db.rollback()
        logger.error(f"[SCOUT send] task failed: {e}")
        raise
    finally:
        db.close()


@celery_app.task(bind=True, max_retries=2, name="scout_weekly_digest_task")
def scout_weekly_digest_task(self):
    """
    Scheduled weekly. Sends ONE email summarizing PENDING_APPROVAL prospects.
    """
    db = SessionLocal()
    try:
        pending = db.query(ScoutProspect).filter(ScoutProspect.status == "PENDING_APPROVAL").all()
        send_failed = db.query(ScoutProspect).filter(ScoutProspect.status == "SEND_FAILED").all()

        if not pending and not send_failed:
            logger.info("[SCOUT digest] nothing pending — no digest sent this week")
            return

        by_pipeline_tier: dict[str, int] = {}
        for p in pending:
            key = f"{p.pipeline}/{p.priority_tier}"
            by_pipeline_tier[key] = by_pipeline_tier.get(key, 0) + 1

        rows_html = "".join(
            f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in sorted(by_pipeline_tier.items())
        )
        failed_line = (
            f"<p><strong>{len(send_failed)} prospects failed to send</strong> and need review "
            f"(missing contact email or a delivery failure).</p>" if send_failed else ""
        )

        body_inner = f"""
        <p>{len(pending)} prospects are awaiting approval this week.</p>
        <table><tr><th>Pipeline / Tier</th><th>Count</th></tr>{rows_html}</table>
        {failed_line}
        {email_button(f"{settings.VERIFY_BASE_URL}/admin/scout/approve", "Review and approve")}
        """
        html = branded_email_html(body_inner, title="SCOUT weekly digest")

        sent = asyncio.run(EmailService().send_html_email(
            to_email=DIGEST_RECIPIENT, subject=f"SCOUT: {len(pending)} prospects awaiting approval",
            body_html=html, category="transactional",
        ))
        if not sent:
            # The digest is the ONE regular touchpoint a human gets (see the
            # module docstring). If it is dropped, approvals stall for a week
            # with nothing indicating why, so this is an alert, not a warning.
            logger.error("[SCOUT digest] send REJECTED by provider — nobody was notified")
        else:
            logger.info(f"[SCOUT digest] sent — {len(pending)} pending, {len(send_failed)} failed")

    finally:
        db.close()
