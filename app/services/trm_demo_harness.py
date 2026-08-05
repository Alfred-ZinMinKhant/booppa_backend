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
#
# `{notice}` is substituted with the Notice that actually binds the demo tenant's
# licence class. It is NOT hardcoded to FSM-N05/N06: those bind banks, and the
# acceptance matrix deliberately includes a payment licensee and an entity with
# no licence confirmed. Where no Notice resolves, the substitution degrades to
# Guidelines wording rather than naming a Notice that does not bind.
SEEDED_GAP: dict[str, dict[str, str]] = {
    "Cyber Security": {
        "gap_analysis": (
            "MFA is enforced for customer-facing logins but not for internal admin "
            "consoles, and patch SLAs are undocumented. {notice} requires MFA "
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
            "drill. {notice} requires major incidents to be notified to MAS within "
            "1 hour of discovery. Tested evidence that would close this: a dated tabletop or "
            "live drill log showing detection-to-notification time under 60 minutes, signed "
            "off by the incident commander — not merely the escalation policy document."
        ),
        "risk_rating": "high",
        "status": "gap",
    },
    "Business Continuity and Disaster Recovery": {
        "gap_analysis": (
            "A DR plan exists with a documented 4-hour RTO for critical systems per "
            "{notice}, and it was tested with an annual failover exercise. MAS treats an "
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


def _seeded_notice_phrase(control: TrmControl, db) -> str:
    """Subject phrase for `{notice}` in SEEDED_GAP, correct for this tenant.

    Resolves the binding Notice for the org's licence class. When none binds
    (payment licensee on the TRM half, unmapped class, licence unconfirmed) it
    degrades to the Guidelines, which are guidance and bind no one specifically
    — never to a Notice number that would be false for this entity.
    """
    from app.trm_workflow_service import (_DOMAIN_NOTICE_MAP,
                                          _organisation_licence_type)
    from app.services import mas_notice_registry as registry

    entry = _DOMAIN_NOTICE_MAP.get(control.domain)
    if entry is not None:
        family, _ = entry
        resolver = (
            registry.cyber_hygiene_instrument if family == "cyber_hygiene"
            else registry.technology_risk_instrument
        )
        try:
            return resolver(_organisation_licence_type(control, db)).citation
        except (ValueError, KeyError):
            pass
    return "The MAS Guidelines on Technology Risk Management"


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
    control.gap_analysis = seeded["gap_analysis"].format(
        notice=_seeded_notice_phrase(control, db)
    )
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
    mas_licence_type: Optional[str] = None,
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

    `mas_licence_type` seeds the org's MAS licence class. `None` is a real
    state, not a missing argument: it is the unconfirmed case that gates the
    Outsourcing Risk Register, and the acceptance matrix exercises it directly.
    Unrecognised values normalise to `None` rather than to a plausible guess.

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

        # Set explicitly (including back to None) so a re-run against the same
        # demo tenant with a different licence really re-branches the register
        # rather than inheriting the previous run's regime.
        from app.services.mas_licence import normalise_licence

        org.mas_licence_type = normalise_licence(mas_licence_type)
        db.commit()

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

        acra = _verify_identity_with_acra(user, db, uen=uen, company_name=company_name)

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
            "mas_licence_type": org.mas_licence_type,
            "acra": acra,
        })
        return result
    finally:
        if owns_db:
            db.close()


def _verify_identity_with_acra(user, db, *, uen: Optional[str], company_name: str) -> dict:
    """Resolve the demo tenant's identity against the **live** ACRA registry.

    The previous version wrote whatever UEN string the caller typed straight onto
    the account via `record_verified_identity`. That is the one thing a demo must
    not do: an unverified number rendered on a cover under the heading "UEN" is a
    fabricated registration, and it looks exactly like a verified one. Here the
    number is only a *query* — nothing is persisted unless data.gov.sg returns a
    matching entity, and `resolve_legal_name` (the single sanctioned account write
    path) is what does the persisting.

    A miss is reported, not papered over: the cover then renders the `[UEN]`
    placeholder, which is the honest output for an entity ACRA does not know.
    """
    from app.services.evidence_enricher import (ACRA_SYNC_RESOLVE_TIMEOUT,
                                                _run_coro_blocking, resolve_legal_name,
                                                trusted_cached_uen)

    # Gated on an explicit UEN: with none supplied there is nothing new to resolve
    # here, and the baseline's own `display_legal_name` already does the
    # name-only ACRA attempt. Adding a second one would just double the lookups.
    if not uen:
        return {"attempted": False,
                "verified": bool(trusted_cached_uen(user, company_hint=company_name))}

    try:
        resolved = _run_coro_blocking(
            resolve_legal_name(
                user, db, company_hint=company_name, uen=uen,
                website_hint=getattr(user, "website", None),
            ),
            timeout=ACRA_SYNC_RESOLVE_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 — network/registry failure, never fatal
        db.rollback()
        logger.warning("[TRMDemo] ACRA resolution failed for %r: %s", company_name, exc)
        return {"attempted": True, "verified": False, "error": str(exc)}

    db.refresh(user)
    # Read back through the trust gate, never off the column — see
    # trusted_cached_uen's docstring for why the raw read is banned.
    confirmed = trusted_cached_uen(user, company_hint=company_name)
    verified = bool(confirmed)
    return {
        "attempted": True,
        "verified": verified,
        "uen": confirmed,
        "legal_name": resolved,
        # Stated plainly so a demo readout can never be mistaken for a registry hit.
        "note": None if verified else (
            "No ACRA match — the cover renders the [UEN] placeholder. Seed a real "
            "Singapore UEN for a customer-facing sample."
        ),
    }


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


# ── document pack ────────────────────────────────────────────────────────────

def seed_and_generate_document_pack(
    *,
    customer_email: Optional[str] = None,
    company_name: str = "NovaPay Fintech Pte Ltd",
    uen: Optional[str] = None,
    mas_licence_type: Optional[str] = None,
    live_ai: bool = True,
    capture_pdf: bool = True,
    db=None,
) -> dict[str, Any]:
    """Seed a demo tenant and run the **real** document-pack worker against it.

    Same shape as `seed_and_generate` and deliberately built on top of it: the
    pack is generated from the same control view as the baseline tracker, so
    running them from one seed is the only way the acceptance evidence shows the
    two agreeing about what is documented vs tested.

    `capture_pdf=True` (the default here, unlike the baseline harness) keeps the
    run from touching S3, the chain or anyone's inbox — the pack email carries
    seven download links, and a demo run that mails them is a demo run that
    mailed a customer. Returns the PDF bytes instead.

    Returns `{pack_id, status, outsourcing_regime, mas_licence_type, user_id,
    user_email, org_id, documents: {doc_type: {title, sha256, word_count,
    pdf_bytes, skipped, skip_reason}}}`.
    """
    from contextlib import ExitStack
    from unittest.mock import patch

    owns_db = db is None
    db = db or SessionLocal()
    try:
        seed = seed_and_generate(
            customer_email=customer_email,
            company_name=company_name,
            uen=uen,
            live_ai=live_ai,
            capture_pdf=True,   # never mail the baseline from the pack harness
            mas_licence_type=mas_licence_type,
            db=db,
        )

        from app.workers.trm_doc_tasks import generate_trm_document_pack

        captured: dict[str, bytes] = {}

        def _fake_upload(pdf_bytes: bytes, key: str) -> str:
            # key is trm/{org}/packs/{pack}/{doc_type}.pdf
            captured[key.rsplit("/", 1)[-1].removesuffix(".pdf")] = pdf_bytes
            return f"https://s3.example/{key}"

        async def _fake_email(self, to_email, subject, body_html, **kwargs):
            return True

        def _no_anchor(sha256_hex, metadata):
            # Not a fabricated tx: a demo must not spend gas, and a plausible-
            # looking hash in a demo pack is exactly the fake that survives
            # into a screenshot. No anchor is the honest value.
            return None, None

        with ExitStack() as stack:
            if capture_pdf:
                stack.enter_context(
                    patch("app.services.trm_pack_delivery.upload_pack_pdf", _fake_upload))
                stack.enter_context(
                    patch("app.services.trm_pack_delivery.anchor_document", _no_anchor))
                stack.enter_context(
                    patch("app.services.email_service.EmailService.send_html_email",
                          _fake_email))
            outcome = generate_trm_document_pack(
                str(seed["user_id"]),
                override_company=company_name,
                bypass_idempotency=True,
            )

        pack = _load_pack(db, outcome.get("pack_id") if isinstance(outcome, dict) else None)
        documents: dict[str, Any] = {}
        for doc_type, entry in ((pack.documents if pack else None) or {}).items():
            documents[doc_type] = {
                "title": entry.get("title"),
                "sha256": entry.get("sha256"),
                "word_count": entry.get("word_count"),
                "skipped": bool(entry.get("skipped")),
                "skip_reason": entry.get("skip_reason"),
                "error": entry.get("error"),
                "pdf_bytes": captured.get(doc_type),
            }

        return {
            "pack_id": (str(pack.id) if pack else None),
            "status": (pack.status if pack else "unknown"),
            "outsourcing_regime": (pack.outsourcing_regime if pack else None),
            "mas_licence_type": (pack.mas_licence_type if pack else None),
            "total_cost_usd": (pack.total_cost_usd if pack else None),
            "user_id": seed["user_id"],
            "user_email": seed["user_email"],
            "org_id": seed["org_id"],
            "deepseek_live": seed["deepseek_live"],
            # Carried through so the pack readout says whether the cover carries a
            # registry-verified UEN or the placeholder — never leaves it ambiguous.
            "acra": seed.get("acra"),
            "documents": documents,
        }
    finally:
        if owns_db:
            db.close()


def _load_pack(db, pack_id: Optional[str]):
    from app.core.models import TrmDocumentPack

    if not pack_id:
        return None
    db.expire_all()
    return db.query(TrmDocumentPack).filter(TrmDocumentPack.id == pack_id).first()


# The acceptance matrix Gianpaolo asked for: one bank-category customer, one
# non-bank FI, and — added because it is the state every existing subscriber is
# currently in — one with no licence confirmed. Case D was added alongside the
# per-licence Notice fix: an insurer is a fully-mapped non-bank class, which
# proves the pack is complete without being bank-scoped.
#
# `expect_docs` is per-licence, not a constant. An MPI resolves its cyber-hygiene
# Notice (FSM-N14) and its outsourcing regime (non-bank Guidelines) but has no
# mapped technology-risk Notice, so it receives 4 of 7 and reports `blocked` —
# partial delivery with an honest ask, not a pack that guesses.
#
# Cases E-G were added when the `insurer` key was split against MAS's "Applies
# to" lists. Case D alone could not catch that split: it passed both before and
# after, because a direct insurer's mapping did not change. The three classes
# that were silently wrong under the old single key are the ones worth running —
# a captive insurer and a general insurance agent were being handed a pack
# asserting FSM-N03 bound them, and a marine mutual was collecting both Notices
# while appearing in the "Applies to" of neither.
#
# Case F is the case that looks like a bug and is not: two documents out of
# seven. A marine mutual resolves no technology-risk and no cyber-hygiene
# Notice, so all five Notice-scoped documents gate and only the Outsourcing Risk
# Register (non-bank Guidelines, which do reach it) and the IT Audit log — the
# one document scoped to no instrument at all — can be issued. Do not "fix" this
# by mapping it to the nearest-looking Notice.
ACCEPTANCE_CASES: tuple[dict[str, Any], ...] = (
    {"case": "A", "company_name": "Meridian Bank (Singapore) Pte Ltd",
     "mas_licence_type": "bank",
     "expect_regime": "bank_notices", "expect_status": "ready", "expect_docs": 7},
    {"case": "B", "company_name": "NovaPay Payments Pte Ltd",
     "mas_licence_type": "mpi",
     "expect_regime": "non_bank_guidelines",
     "expect_status": "blocked_licence_unknown", "expect_docs": 4},
    {"case": "C", "company_name": "Harbourline Capital Pte Ltd",
     "mas_licence_type": None,
     "expect_regime": None, "expect_status": "blocked_licence_unknown", "expect_docs": 1},
    {"case": "D", "company_name": "Straitsure Insurance Pte Ltd",
     "mas_licence_type": "insurer",
     "expect_regime": "non_bank_guidelines", "expect_status": "ready", "expect_docs": 7},
    # FSM-N04 reaches captives; FSM-N03 does not. Same 4-of-7 shape as the MPI.
    {"case": "E", "company_name": "Keppel Captive Insurance Pte Ltd",
     "mas_licence_type": "captive_insurer",
     "expect_regime": "non_bank_guidelines",
     "expect_status": "blocked_licence_unknown", "expect_docs": 4},
    # Neither Notice lists marine mutuals. Only the two documents that need no
    # Notice survive — see the note above before changing this number.
    {"case": "F", "company_name": "Straits Marine Mutual Association",
     "mas_licence_type": "marine_mutual_insurer",
     "expect_regime": "non_bank_guidelines",
     "expect_status": "blocked_licence_unknown", "expect_docs": 2},
    # FSM-N31 (FSM Act 2022 DTSP regime) resolves for cyber hygiene; no
    # FSM-series TRM Notice was found for DTSPs, so technology risk gates.
    {"case": "G", "company_name": "Lumen Digital Assets Pte Ltd",
     "mas_licence_type": "dtsp",
     "expect_regime": "non_bank_guidelines",
     "expect_status": "blocked_licence_unknown", "expect_docs": 4},
)


def run_acceptance_matrix(*, live_ai: bool = True, db=None) -> dict[str, Any]:
    """Run every acceptance case and report pass/fail per case.

    Each case gets a **fresh** tenant (no `customer_email`), because reusing one
    would let a previous case's register survive into the next and make a branch
    failure look like a pass.
    """
    results: list[dict[str, Any]] = []
    for case in ACCEPTANCE_CASES:
        out = seed_and_generate_document_pack(
            company_name=case["company_name"],
            mas_licence_type=case["mas_licence_type"],
            live_ai=live_ai,
            db=db,
        )
        delivered = [
            dt for dt, e in out["documents"].items()
            if e.get("pdf_bytes") and not e.get("skipped")
        ]
        register = out["documents"].get("outsourcing_risk_register") or {}
        checks = {
            "status": out["status"] == case["expect_status"],
            "regime": out["outsourcing_regime"] == case["expect_regime"],
            "document_count": len(delivered) == case["expect_docs"],
            "register_gated": (
                register.get("skipped") is True
                if case["mas_licence_type"] is None
                else bool(register.get("pdf_bytes"))
            ),
        }
        results.append({
            **out,
            "case": case["case"],
            "company_name": case["company_name"],
            "expected": case,
            "delivered_doc_types": sorted(delivered),
            "checks": checks,
            "passed": all(checks.values()),
        })
    return {"passed": all(r["passed"] for r in results), "cases": results}


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
