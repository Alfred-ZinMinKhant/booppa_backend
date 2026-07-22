#!/usr/bin/env python3
"""
scripts/demo_trm_baseline.py — Phase 4 demonstration harness

Seeds a fintech user + org, initialises all 13 MAS TRM domains, runs gap
analysis on the three domains with binding statutory notices (Cyber Security /
TRM-5, Incident Management / TRM-8, Business Continuity & DR / TRM-10),
attaches tested + documented evidence, and regenerates the baseline PDF so
the artifact can be reviewed directly.

Uses live DeepSeek if DEEPSEEK_API_KEY is set; otherwise falls back to
deterministic, notice-specific seeded narratives so the harness runs offline.

Usage:
    python scripts/demo_trm_baseline.py
"""
import asyncio
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import SessionLocal
from app.core.models import User, Organisation, TrmControl, TrmEvidence, MAS_TRM_DOMAINS
from app.core.config import settings

SCRATCHPAD = Path(
    "/private/tmp/claude-501/-Users-zinminkhant-Documents-Booppa-Booppa-booppa-backend/"
    "aca59fe5-95e8-4e2d-a690-bb8d9af2599e/scratchpad"
)

# Deterministic fallback narratives — notice-specific, matches the shape the
# sharpened prompt (Phase 3) asks DeepSeek to produce.
_SEEDED_GAP = {
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

_DEMO_DOMAINS = ["Cyber Security", "Incident Management", "Business Continuity and Disaster Recovery"]


async def _run_gap(control, context, db):
    if settings.DEEPSEEK_API_KEY:
        from app.trm_workflow_service import run_gap_analysis
        try:
            return await run_gap_analysis(control, context, db)
        except Exception as exc:
            # Live call failed (e.g. model emitted malformed JSON) — fall back to
            # the seeded narrative so the harness still produces a full artifact.
            print(f"  [warn] live DeepSeek call for {control.domain} failed ({exc}); "
                  f"using seeded fallback", file=sys.stderr)
    seeded = _SEEDED_GAP[control.domain]
    control.gap_analysis = seeded["gap_analysis"]
    control.risk_rating = seeded["risk_rating"]
    control.status = seeded["status"]
    control.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(control)
    return control


def main():
    db = SessionLocal()
    try:
        suffix = uuid.uuid4().hex[:8]
        user = User(
            id=uuid.uuid4(),
            email=f"demo-trm-{suffix}@booppa.io",
            hashed_password="not-a-real-hash",
            role="VENDOR",
            plan="pro_suite",
            company="NovaPay Fintech Pte Ltd",
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        org = Organisation(
            id=uuid.uuid4(),
            name=user.company,
            slug=f"novapay-{suffix}",
            owner_user_id=user.id,
        )
        db.add(org)
        db.commit()
        db.refresh(org)

        from app.trm_workflow_service import initialise_trm_controls, _DOMAIN_REFS
        controls = initialise_trm_controls(str(org.id), db)
        by_domain = {c.domain: c for c in controls}

        contexts = {
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

        for domain in _DEMO_DOMAINS:
            ctrl = by_domain[domain]
            asyncio.run(_run_gap(ctrl, contexts[domain], db))

        # Cyber Security — documented-only evidence (MFA + patch policy docs).
        cyber = by_domain["Cyber Security"]
        db.add(TrmEvidence(
            id=uuid.uuid4(), control_id=cyber.id, file_name="mfa_policy.pdf",
            hash_value=uuid.uuid4().hex + uuid.uuid4().hex[:32],
            evidence_type="documented",
        ))
        db.add(TrmEvidence(
            id=uuid.uuid4(), control_id=cyber.id, file_name="patch_management_sop.pdf",
            hash_value=uuid.uuid4().hex + uuid.uuid4().hex[:32],
            evidence_type="documented",
        ))

        # Incident Management — documented plan only (no drill yet — honest gap).
        incident = by_domain["Incident Management"]
        db.add(TrmEvidence(
            id=uuid.uuid4(), control_id=incident.id, file_name="incident_response_runbook.pdf",
            hash_value=uuid.uuid4().hex + uuid.uuid4().hex[:32],
            evidence_type="documented",
        ))

        # BCP/DR — tested evidence, the MAS-defensible case.
        bcdr = by_domain["Business Continuity and Disaster Recovery"]
        db.add(TrmEvidence(
            id=uuid.uuid4(), control_id=bcdr.id, file_name="dr_failover_test_2026-03.pdf",
            hash_value=uuid.uuid4().hex + uuid.uuid4().hex[:32],
            tx_hash="0x" + uuid.uuid4().hex + uuid.uuid4().hex[:24],
            evidence_type="tested",
            tested_at=datetime(2026, 3, 15),
            attestation="Annual DR failover test — 3h58m recovery of core ledger, verified by Head of IT.",
        ))
        db.commit()

        # Regenerate the baseline via the real worker path so the artifact matches
        # exactly what a paying customer receives.
        captured = {}

        def _fake_upload_pdf(pdf_bytes, report_id):
            captured["pdf"] = pdf_bytes
            return f"https://s3.example/{report_id}.pdf"

        async def _fake_upload(self, pdf_bytes, report_id):
            return _fake_upload_pdf(pdf_bytes, report_id)

        async def _fake_email(self, to_email, subject, body_html):
            return True

        from unittest.mock import patch
        with patch("app.services.storage.S3Service.upload_pdf", _fake_upload), \
             patch("app.services.email_service.EmailService.send_html_email", _fake_email):
            from app.workers.tasks import run_suite_trm_baseline_for_user
            run_suite_trm_baseline_for_user(str(user.id))

        pdf_bytes = captured.get("pdf")
        if not pdf_bytes:
            print("ERROR: baseline task did not produce a PDF.", file=sys.stderr)
            sys.exit(1)

        SCRATCHPAD.mkdir(parents=True, exist_ok=True)
        out_path = SCRATCHPAD / f"trm_baseline_demo_{suffix}.pdf"
        out_path.write_bytes(pdf_bytes)

        print(f"Baseline PDF written to: {out_path}")
        print(f"Demo user: {user.email}  org: {org.name}  id: {org.id}")
        print(f"DeepSeek live: {bool(settings.DEEPSEEK_API_KEY)}")
        print("\nThree-layer readout:")
        print("  1. Promise      — 13-domain baseline, honest not-a-statement-of-compliance framing.")
        print("  2. Customer need — evidence attached and visible per domain (documented vs tested).")
        print("  3. MAS requirement — TRM-5/8/10 narratives now cite Notice 655/FSM-N06 (MFA/patching)")
        print("                        and Notice 644/FSM-N05 (1h notification / 4h recovery); BCP/DR")
        print("                        shows a dated, attested, blockchain-anchored test — the")
        print("                        inspection-defensible case MAS distinguishes from a plan doc.")
        print("  Residual gap    — Incident Management has a documented runbook but no drilled")
        print("                        1-hour notification test yet; correctly renders as a gap,")
        print("                        not falsely marked compliant.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
