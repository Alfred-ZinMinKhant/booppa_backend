"""Reusable MAS TRM evidence demonstration harness.

Seeds a fintech tenant + org, initialises all 13 MAS TRM domains, runs gap
analysis on the three domains carrying binding statutory notices (Cyber
Security / TRM-5, Incident Management / TRM-8, Business Continuity & DR /
TRM-10), attaches documented + tested evidence, and regenerates the baseline
through the **real** worker path so the artifact is byte-identical in structure
to what a paying customer receives.

One code path, two front doors: `scripts/demo_trm_baseline.py` (shell) and
`POST /admin/trm/demo-baseline` (admin panel) both call `seed_and_generate`.

Live DeepSeek is used when `DEEPSEEK_API_KEY` is set and `live_ai` is not
disabled; otherwise deterministic, notice-specific seeded narratives keep the
harness runnable offline.
"""
import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any, Optional

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.models import Organisation, TrmControl, TrmEvidence, User

logger = logging.getLogger(__name__)

DEMO_DOMAINS = [
    "Cyber Security",
    "Incident Management",
    "Business Continuity and Disaster Recovery",
]

# Deterministic fallback narratives — notice-specific, matching the shape the
# sharpened prompt asks DeepSeek to produce.
SEEDED_GAP: dict[str, dict[str, str]] = {
    "Cyber Security": {
        "gap_analysis": (
            "MFA is enforced for customer-facing logins but not for internal admin "
            "consoles, and patch SLAs are undocumented. Notice 655/FSM-N06 requires MFA "
            "and rapid patching as mandatory controls, not best practice. Tested evidence "
            "that would close this: a dated privileged-access MFA rollout confirmation and "
            "a patch-cadence report showing critical CVEs remediated within SLA, not just "
            "a written patch policy."
        ),
        "risk_rating": "high",
        "status": "gap",
    },
    "Incident Management": {
        "gap_analysis": (
            "An incident response plan exists but has no verified 1-hour MAS notification "
            "drill. Notice 644/FSM-N05 requires major incidents to be notified to MAS within "
            "1 hour of discovery. Tested evidence that would close this: a dated tabletop or "
            "live drill log showing detection-to-notification time under 60 minutes, signed "
            "off by the incident commander — not merely the escalation policy document."
        ),
        "risk_rating": "high",
        "status": "gap",
    },
    "Business Continuity and Disaster Recovery": {
        "gap_analysis": (
            "A DR plan exists with a documented 4-hour RTO for critical systems per Notice "
            "644/FSM-N05, and it was tested with an annual failover exercise. MAS treats an "
            "untested BCP/DR plan as an aspiration, not a control — the dated test result is "
            "what closes this gap, distinct from the plan document itself."
        ),
        "risk_rating": "medium",
        "status": "in_progress",
    },
}

DEMO_CONTEXTS: dict[str, str] = {
    "Cyber Security": (
        "MFA is enforced for customer-facing web/app logins via TOTP. Internal admin "
        "consoles rely on password + IP allowlist only. No documented patch SLA; "
        "patches are applied ad hoc when the ops team notices a vendor advisory."
    ),
    "Incident Management": (
        "We have a written incident response runbook with severity tiers (P1-P4) and "
        "an on-call rotation. MAS notification is mentioned as 'as soon as possible' "
        "with no drilled timeline; no incident has required MAS notification yet."
    ),
    "Business Continuity and Disaster Recovery": (
        "DR plan targets a 4-hour RTO for the core ledger and payments services, "
        "replicated cross-AZ. An annual failover drill was run in March 2026, "
        "recovering the core ledger in 3h58m, signed off by the Head of IT."
    ),
}

DR_TEST_DATE = datetime(2026, 3, 15)


def _fake_hash() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex[:32]


async def _run_gap(control: TrmControl, context: str, db, live_ai: bool) -> TrmControl:
    if live_ai and settings.DEEPSEEK_API_KEY:
        from app.trm_workflow_service import run_gap_analysis
        try:
            return await run_gap_analysis(control, context, db)
        except Exception as exc:
            # Live call failed (e.g. the model emitted malformed JSON) — fall back
            # to the seeded narrative so the harness still produces a full artifact.
            logger.warning("[TRMDemo] live gap analysis for %s failed (%s); using seeded fallback",
                           control.domain, exc)
    seeded = SEEDED_GAP[control.domain]
    control.gap_analysis = seeded["gap_analysis"]
    control.risk_rating = seeded["risk_rating"]
    control.status = seeded["status"]
    control.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(control)
    return control


def seed_and_generate(
    *,
    customer_email: Optional[str] = None,
    company_name: str = "NovaPay Fintech Pte Ltd",
    uen: Optional[str] = None,
    live_ai: bool = True,
    capture_pdf: bool = False,
    db=None,
) -> dict[str, Any]:
    """Seed the evidence-graded demo tenant and regenerate its TRM baseline.

    `customer_email` reuses an existing account when one matches (so repeated QA
    runs against the same address don't pile up tenants); otherwise a throwaway
    `demo-trm-<suffix>@booppa.io` user is created.

    `capture_pdf=True` intercepts the S3 upload and email send and returns the
    raw bytes instead — the shell script's mode, which must not mail anyone.
    Otherwise the real upload/email path runs and `download_url` is a live
    presigned link.

    Returns `{download_url, pdf_bytes, user_id, user_email, org_id,
    domains_analysed, evidence_summary, deepseek_live}`.
    """
    owns_db = db is None
    db = db or SessionLocal()
    try:
        suffix = uuid.uuid4().hex[:8]

        user = None
        if customer_email:
            user = db.query(User).filter(User.email == customer_email).first()
        if not user:
            user = User(
                id=uuid.uuid4(),
                email=customer_email or f"demo-trm-{suffix}@booppa.io",
                hashed_password="not-a-real-hash",
                role="VENDOR",
                plan="pro_suite",
                company=company_name,
            )
            db.add(user)
            db.commit()
            db.refresh(user)

        # The worker picks the *earliest* org owned by the user, so reuse that one
        # rather than adding a second org the baseline would never read.
        org = (
            db.query(Organisation)
            .filter(Organisation.owner_user_id == user.id)
            .order_by(Organisation.created_at.asc())
            .first()
        )
        if not org:
            org = Organisation(
                id=uuid.uuid4(),
                name=company_name,
                slug=f"trm-demo-{suffix}",
                owner_user_id=user.id,
            )
            db.add(org)
            db.commit()
            db.refresh(org)

        from app.trm_workflow_service import initialise_trm_controls

        controls = db.query(TrmControl).filter(TrmControl.organisation_id == org.id).all()
        if not controls:
            controls = initialise_trm_controls(str(org.id), db)
        by_domain = {c.domain: c for c in controls}

        for domain in DEMO_DOMAINS:
            ctrl = by_domain.get(domain)
            if ctrl is None:
                continue
            asyncio.run(_run_gap(ctrl, DEMO_CONTEXTS[domain], db, live_ai))

        # Re-running must not double the evidence register — clear what this
        # harness previously attached to the three demo controls, then re-seed.
        demo_ids = [by_domain[d].id for d in DEMO_DOMAINS if d in by_domain]
        if demo_ids:
            (db.query(TrmEvidence)
               .filter(TrmEvidence.control_id.in_(demo_ids))
               .delete(synchronize_session=False))
            db.commit()

        # Cyber Security — documented-only evidence (MFA + patch policy docs).
        if "Cyber Security" in by_domain:
            cyber_id = by_domain["Cyber Security"].id
            for fname in ("mfa_policy.pdf", "patch_management_sop.pdf"):
                db.add(TrmEvidence(
                    id=uuid.uuid4(), control_id=cyber_id, file_name=fname,
                    hash_value=_fake_hash(), evidence_type="documented",
                ))

        # Incident Management — documented plan only. No drill yet: this is the
        # honest residual gap, and it must not render as compliant.
        if "Incident Management" in by_domain:
            db.add(TrmEvidence(
                id=uuid.uuid4(), control_id=by_domain["Incident Management"].id,
                file_name="incident_response_runbook.pdf",
                hash_value=_fake_hash(), evidence_type="documented",
            ))

        # BCP/DR — tested, dated, attested, anchored. The MAS-defensible case.
        if "Business Continuity and Disaster Recovery" in by_domain:
            db.add(TrmEvidence(
                id=uuid.uuid4(),
                control_id=by_domain["Business Continuity and Disaster Recovery"].id,
                file_name="dr_failover_test_2026-03.pdf",
                hash_value=_fake_hash(),
                tx_hash="0x" + uuid.uuid4().hex + uuid.uuid4().hex[:24],
                evidence_type="tested",
                tested_at=DR_TEST_DATE,
                attestation=(
                    "Annual DR failover test — 3h58m recovery of core ledger, "
                    "verified by Head of IT."
                ),
            ))
        db.commit()

        if uen:
            # Give the ACRA/legal-name resolver something to work with so the
            # cover stamps a registered legal name, not a raw domain.
            try:
                if hasattr(user, "uen") and not user.uen:
                    user.uen = uen
                    db.commit()
            except Exception:
                db.rollback()

        result = _generate(user, org, company_name, capture_pdf, db)
        result.update({
            "user_id": str(user.id),
            "user_email": user.email,
            "org_id": str(org.id),
            "domains_analysed": list(DEMO_DOMAINS),
            "evidence_summary": {
                "Cyber Security": "Documented (2)",
                "Incident Management": "Documented (1) — no drill on file (residual gap)",
                "Business Continuity and Disaster Recovery":
                    f"Tested — {DR_TEST_DATE:%d %b %Y}",
            },
            "deepseek_live": bool(live_ai and settings.DEEPSEEK_API_KEY),
        })
        return result
    finally:
        if owns_db:
            db.close()


def _generate(user, org, company_name: str, capture_pdf: bool, db) -> dict[str, Any]:
    """Run the real baseline worker; return `{download_url, pdf_bytes}`."""
    from app.workers.tasks import run_suite_trm_baseline_for_user

    if capture_pdf:
        from unittest.mock import patch

        captured: dict[str, bytes] = {}

        async def _fake_upload(self, pdf_bytes, report_id):
            captured["pdf"] = pdf_bytes
            return f"https://s3.example/{report_id}.pdf"

        async def _fake_email(self, to_email, subject, body_html):
            return True

        with patch("app.services.storage.S3Service.upload_pdf", _fake_upload), \
             patch("app.services.email_service.EmailService.send_html_email", _fake_email):
            run_suite_trm_baseline_for_user(
                str(user.id), override_company=company_name, bypass_idempotency=True
            )
        return {"pdf_bytes": captured.get("pdf"), "download_url": None}

    run_suite_trm_baseline_for_user(
        str(user.id), override_company=company_name, bypass_idempotency=True
    )
    return {"pdf_bytes": None, "download_url": latest_baseline_url(str(user.id), db)}


def latest_baseline_url(user_id: str, db) -> Optional[str]:
    """Freshly presigned URL for the newest completed TRM baseline, or None.

    Mirrors `GET /vendor/trm/baseline/latest` — presigns expire in 7 days, so the
    stored URL is only a fallback.
    """
    from app.core.models import Report

    row = (
        db.query(Report)
        .filter(
            Report.owner_id == user_id,
            Report.framework == "trm_baseline",
            Report.status == "completed",
        )
        .order_by(Report.completed_at.desc().nullslast())
        .first()
    )
    if not row:
        return None
    ad = row.assessment_data if isinstance(row.assessment_data, dict) else {}
    key = row.file_key or ad.get("s3_key")
    url = row.s3_url or ad.get("s3_url")
    if key:
        try:
            from app.services.storage import S3Service
            s3 = S3Service()
            return s3.s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": s3.bucket, "Key": key},
                ExpiresIn=604800,
            )
        except Exception as exc:
            logger.warning("[TRMDemo] re-presign failed for %s: %s", user_id, exc)
    return url
