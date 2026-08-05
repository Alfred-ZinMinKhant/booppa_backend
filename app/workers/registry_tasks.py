"""Scheduled re-verification reminder for the regulatory citation registry.

Every instrument in `regulatory_registry` carries the date a human last checked
it against the issuing body's own publication. Nothing keeps that date honest by
itself, so this task reads it weekly and emails ops the rows that are past due
or were never verified at all.

**Detection only.** This task must never write to the registry. An automated
"still current" tick would make every row look verified while nobody had looked
at anything — which is precisely the failure the per-row date replaced (the MAS
table carried one module-level `REGISTRY_VERIFIED_ON` while several of its
`applies_to` sets were wrong). The alert is the product; the fix is a human
opening the regulator's page and moving the date in a commit.

Weekly, not daily: the list changes slowly and a daily mail about the same six
rows is a mail people stop opening.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="registry.check_citation_staleness", queue="fast_queue")
def check_citation_staleness(max_age_days: int = 0):
    """Report instruments due for re-verification. Returns the refs it found."""
    from app.core.config import settings
    from app.services.email_service import EmailService
    from app.services.regulatory_registry import (
        DEFAULT_MAX_AGE_DAYS,
        stale_instruments,
    )

    limit = max_age_days or DEFAULT_MAX_AGE_DAYS
    stale = stale_instruments(max_age_days=limit)
    if not stale:
        logger.info("[CitationRegistry] all instruments verified within %sd", limit)
        return {"stale": 0, "refs": []}

    refs = [inst.ref for inst, _ in stale]
    logger.warning(
        "[CitationRegistry] %d instrument(s) due for re-verification: %s",
        len(stale), ", ".join(refs),
    )

    def _source_cell(inst) -> str:
        # The page the row was last read against. Rows that have never been
        # verified have none — say so rather than leaving a blank cell, because
        # "no source recorded" is the actual finding for those.
        if inst.source_url:
            return (
                f"<a href='{inst.source_url}' style='color:#2563eb'>"
                f"{inst.source_url}</a>"
            )
        return "<span style='color:#b45309'>no source recorded</span>"

    rows = "".join(
        f"<tr><td style='padding:6px 10px;border-bottom:1px solid #e2e8f0'>"
        f"<b>{inst.ref}</b><br><span style='color:#475569'>{inst.citation}</span><br>"
        f"<span style='font-size:12px'>{_source_cell(inst)}</span></td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #e2e8f0'>{reason}</td></tr>"
        for inst, reason in stale
    )
    html = (
        f"<p>{len(stale)} instrument(s) in the citation registry are due for "
        f"human re-verification as of {date.today().isoformat()} "
        f"(limit {limit} days).</p>"
        "<p>These citations are printed in signed and hash-anchored customer "
        "documents. Open the issuing body's own page, confirm the instrument is "
        "still in force and that <code>applies_to</code> is still right, then "
        "move <code>verified_on</code> and record the page you read in "
        "<code>source_url</code>, in a commit. "
        "<b>Do not move the date without checking</b> — the date is the "
        "evidence.</p>"
        f"<table style='border-collapse:collapse;font-size:14px'>{rows}</table>"
    )

    try:
        sent = asyncio.run(EmailService().send_html_email(
            to_email=settings.SUPPORT_EMAIL,
            subject=f"[Booppa] {len(stale)} regulatory citations due for re-verification",
            html_content=html,
        ))
        if not sent:
            # send_html_email returns False on provider rejection and does not
            # raise; without this line a rejected alert is indistinguishable
            # from a clean sweep.
            logger.error("[CitationRegistry] staleness alert email was rejected")
    except Exception as exc:  # noqa: BLE001 — alerting must not fail the sweep
        logger.error("[CitationRegistry] staleness alert failed: %s", exc)

    return {"stale": len(stale), "refs": refs}
