"""Pro Suite activation harness — turn the four Pro-exclusive capabilities on and
produce the artifacts that prove they work.

The Pro baseline listed multi-subsidiary, SSO and white-label as "Ready" —
provisioned but never exercised. A document that lists an entitlement is not
evidence the entitlement works, which is what blocked commercial sign-off. This
harness activates all of them against a real tenant and regenerates the baseline
through the **real** worker, so the resulting PDF is what a paying customer gets.

One code path, two front doors: `scripts/demo_pro_suite.py` (shell) and
`POST /admin/pro-suite/demo` (admin panel) both call `activate_pro_features`.

Pro = Standard + four activations, so the parent tenant's evidence-graded
controls come from `trm_demo_harness.seed_and_generate` rather than being
re-seeded here.

What each activation proves:

* **Multi-subsidiary** — two child tenants with *different* completion profiles.
  A rollup where every entity looks identical proves nothing about the rollup;
  the point of a group view is seeing which entity is behind.
* **White-label** — a real `WhiteLabelConfig` + logo, so the regenerated PDF
  carries the customer's brand rather than Booppa's.
* **SSO** — an `SsoConfig` pointing at the mock IdP (`saml_mock_idp.py`), so the
  round trip in `run_sso_roundtrip` exercises the real ACS route.
"""
from __future__ import annotations

import logging
import uuid
from io import BytesIO
from typing import Any, Optional

from app.core.db import SessionLocal
from app.core.models import (
    MAS_TRM_DOMAINS,
    Organisation,
    SsoConfig,
    TrmControl,
    User,
    WhiteLabelConfig,
)
from app.services.trm_demo_harness import _generate, latest_baseline_url

logger = logging.getLogger(__name__)

DEFAULT_COMPANY = "NovaPay Fintech Pte Ltd"

# Two subsidiaries, deliberately uneven. `domains` are the ones marked compliant;
# `gap_domain` is left as an open high-risk control so the rollup's lag alerts
# have something real to fire on.
SUBSIDIARY_PROFILES: list[dict[str, Any]] = [
    {
        "company": "NovaPay Payments (Singapore) Pte Ltd",
        "slug_hint": "novapay-payments",
        "compliant_domains": [
            "Cyber Security",
            "Incident Management",
            "Technology Risk Governance",
            "Authentication and Access Management",
        ],
        "gap_domain": "Business Continuity and Disaster Recovery",
    },
    {
        "company": "NovaPay Lending (Malaysia) Sdn Bhd",
        "slug_hint": "novapay-lending",
        "compliant_domains": ["Cyber Security"],
        "gap_domain": "Incident Management",
    },
]

# Customer branding for the demo. Deliberately nothing like Booppa's palette —
# a branded PDF that still looks like ours is not a demonstration.
DEMO_BRANDING = {
    "primary_color": "#F5B700",     # accent rule
    "secondary_color": "#231F52",   # header band
    "report_header_text": "NovaPay Group Risk & Compliance",
    "footer_text": (
        "Prepared for NovaPay Group Risk & Compliance. Internal use only — "
        "this document is not a statement of MAS compliance."
    ),
}


def _demo_logo_png(text: str = "NOVAPAY") -> bytes:
    """A wordmark PNG standing in for the customer's uploaded logo.

    Drawn light-on-transparent because `pdf_logo.draw_logo_header` places it on
    the dark branding band.
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (840, 300), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 120, 60, 180], fill=(245, 183, 0, 255))
    d.text((80, 130), text, fill=(255, 255, 255, 255))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _ensure_org(db, owner: User, name: str, slug_hint: str) -> Organisation:
    """Earliest org owned by `owner`, created if absent.

    Earliest, because the baseline worker reads only that one — adding a second
    org would leave the baseline looking at a workspace the harness never seeded.
    """
    org = (
        db.query(Organisation)
        .filter(Organisation.owner_user_id == owner.id)
        .order_by(Organisation.created_at.asc())
        .first()
    )
    if org:
        return org
    org = Organisation(
        id=uuid.uuid4(), name=name,
        slug=f"{slug_hint}-{uuid.uuid4().hex[:6]}", owner_user_id=owner.id,
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _seed_subsidiary(db, parent: User, profile: dict[str, Any]) -> dict[str, Any]:
    """Create (or reuse) one child tenant and give it a distinct TRM profile."""
    email = f"{profile['slug_hint']}@demo.booppa.io"
    sub = db.query(User).filter(User.email == email).first()
    if not sub:
        sub = User(
            id=uuid.uuid4(), email=email, hashed_password="not-a-real-hash",
            role="VENDOR", plan=parent.plan, company=profile["company"],
        )
        db.add(sub)
    # Re-running must relink rather than fork a second tenant off the same email.
    sub.parent_user_id = parent.id
    sub.company = profile["company"]
    db.commit()
    db.refresh(sub)

    org = _ensure_org(db, sub, profile["company"], profile["slug_hint"])

    # `initialise_trm_controls` has no idempotency guard — calling it on an org
    # that already has controls yields 26 rows, not 13.
    controls = db.query(TrmControl).filter(TrmControl.organisation_id == org.id).all()
    if not controls:
        from app.trm_workflow_service import initialise_trm_controls
        controls = initialise_trm_controls(str(org.id), db)

    compliant = set(profile["compliant_domains"])
    # A misspelt domain would match no control and silently inflate the reported
    # completion count — the exact kind of number nobody re-checks.
    unknown = (compliant | {profile["gap_domain"]}) - set(MAS_TRM_DOMAINS)
    if unknown:
        raise ValueError(f"Unknown MAS TRM domain(s) in demo profile: {sorted(unknown)}")

    applied = 0
    for c in controls:
        if c.domain in compliant:
            c.status = "compliant"
            c.risk_rating = "low"
            applied += 1
        elif c.domain == profile["gap_domain"]:
            c.status = "gap"
            c.risk_rating = "high"
            c.gap_analysis = (
                f"{profile['company']} has no tested evidence for this domain. "
                "MAS treats a documented plan with no dated test result as an "
                "aspiration, not a control."
            )
        else:
            c.status = "not_started"
            c.risk_rating = None
    db.commit()

    return {
        "id": str(sub.id),
        "email": sub.email,
        "name": profile["company"],
        "org_id": str(org.id),
        "domains_complete": applied,
        "domains_total": len(MAS_TRM_DOMAINS),
        "open_gap_domain": profile["gap_domain"],
    }


def _apply_white_label(db, org: Organisation) -> dict[str, Any]:
    """Upsert the tenant's branding + logo. Returns what the caller can assert on."""
    wl = (
        db.query(WhiteLabelConfig)
        .filter(WhiteLabelConfig.organisation_id == org.id)
        .first()
    )
    if not wl:
        wl = WhiteLabelConfig(id=uuid.uuid4(), organisation_id=org.id)
        db.add(wl)
    wl.primary_color = DEMO_BRANDING["primary_color"]
    wl.secondary_color = DEMO_BRANDING["secondary_color"]
    wl.report_header_text = DEMO_BRANDING["report_header_text"]
    wl.footer_text = DEMO_BRANDING["footer_text"]

    logo_uploaded = False
    try:
        from app.services.storage import S3Service
        s3 = S3Service()
        data = _demo_logo_png()
        key = f"white-label-logos/{org.id}/demo-wordmark.png"
        s3.s3_client.put_object(
            Bucket=s3.bucket, Key=key, Body=data, ContentType="image/png"
        )
        wl.logo_s3_key = key
        logo_uploaded = True
    except Exception as exc:
        # Offline / no S3 credentials. The branding colours and header text still
        # apply, so the demo degrades to "branded, without the logo" rather than
        # failing outright — but say so, don't let the caller assume a logo.
        logger.warning("[ProSuiteDemo] white-label logo upload skipped: %s", exc)

    db.commit()
    db.refresh(wl)
    return {
        "applied": True,
        "logo_uploaded": logo_uploaded,
        "logo_s3_key": wl.logo_s3_key,
        "primary_color": wl.primary_color,
        "secondary_color": wl.secondary_color,
        "report_header_text": wl.report_header_text,
        "footer_text": wl.footer_text,
    }


def _apply_sso(db, org: Organisation, idp_metadata_url: Optional[str]) -> dict[str, Any]:
    """Point the org's SSO config at an IdP and activate it.

    `idp_metadata_url` is the mock IdP's `file://` metadata when the harness
    minted one; a real tenant's config carries their own https metadata URL.
    """
    from app.services.saml_service import sp_acs_url, sp_entity_id
    from app.services.saml_mock_idp import MOCK_IDP_ENTITY_ID

    cfg = db.query(SsoConfig).filter(SsoConfig.organisation_id == org.id).first()
    if not cfg:
        cfg = SsoConfig(id=uuid.uuid4(), organisation_id=org.id)
        db.add(cfg)
    cfg.protocol = "saml"
    cfg.is_active = True
    cfg.sp_acs_url = sp_acs_url(org.slug)
    if idp_metadata_url:
        cfg.idp_metadata_url = idp_metadata_url
        cfg.idp_entity_id = MOCK_IDP_ENTITY_ID
    db.commit()
    db.refresh(cfg)
    return {
        "protocol": cfg.protocol,
        "is_active": bool(cfg.is_active),
        "acs_url": cfg.sp_acs_url,
        "entity_id": sp_entity_id(org.slug),
        "metadata_url": f"{sp_entity_id(org.slug)}",
        "login_url": sp_acs_url(org.slug).replace("/acs/", "/login/"),
        "idp_metadata_url": cfg.idp_metadata_url,
    }


def activate_pro_features(
    *,
    customer_email: Optional[str] = None,
    company_name: str = DEFAULT_COMPANY,
    subsidiary_names: Optional[list[str]] = None,
    live_ai: bool = True,
    capture_pdf: bool = False,
    with_mock_idp: bool = True,
    db=None,
) -> dict[str, Any]:
    """Activate all four Pro-exclusive capabilities and regenerate the baseline.

    `capture_pdf=True` intercepts the S3 upload and email send and returns raw
    bytes — the shell script's mode, which must mail nobody.

    `with_mock_idp=False` skips minting a throwaway IdP (used when the caller
    only wants the tenant state, or when `xmlsec1` isn't installed).

    Returns the activation payload: `{download_url, pdf_bytes, user_id, org_id,
    subsidiaries, white_label, sso, provisioning_status, mock_idp_dir}`. The
    caller owns `mock_idp_dir` cleanup once it is done with the SSO round trip —
    the `SsoConfig` points into it.
    """
    owns_db = db is None
    db = db or SessionLocal()
    mock_idp = None
    try:
        # Parent tenant: Standard's evidence-graded demo, reused wholesale.
        # capture_pdf=True here regardless — this first pass exists to seed, and
        # the artifact the caller wants is the *second* generation, after the Pro
        # activations are in place. Generating twice is cheaper than duplicating
        # the seeding logic and risking the two drifting apart.
        from app.services import trm_demo_harness

        base = trm_demo_harness.seed_and_generate(
            customer_email=customer_email, company_name=company_name,
            live_ai=live_ai, capture_pdf=True, db=db,
        )
        user = db.query(User).filter(User.id == base["user_id"]).first()
        if user is None:
            raise RuntimeError("seed_and_generate did not yield a usable tenant")
        # SSO and white-label are gated on the Pro plan key (billing/enforcement.py);
        # a Standard-plan tenant would render them "Ready" no matter what we seed.
        user.plan = "pro_suite"
        db.commit()

        org = db.query(Organisation).filter(Organisation.id == base["org_id"]).first()

        # Caller-supplied names override the defaults positionally; the completion
        # profiles (which is what makes the rollup non-uniform) always come from
        # SUBSIDIARY_PROFILES.
        profiles = [
            {**p, "company": subsidiary_names[i]}
            if subsidiary_names and i < len(subsidiary_names) else p
            for i, p in enumerate(SUBSIDIARY_PROFILES)
        ]

        subsidiaries = [_seed_subsidiary(db, user, p) for p in profiles]
        white_label = _apply_white_label(db, org)

        idp_metadata_url = None
        if with_mock_idp:
            from app.services.saml_mock_idp import MockIdp, xmlsec1_available
            if xmlsec1_available():
                mock_idp = MockIdp()
                idp_metadata_url = mock_idp.metadata_url
            else:
                logger.warning(
                    "[ProSuiteDemo] xmlsec1 not installed — SSO config is activated "
                    "but no mock IdP was minted, so the round trip cannot run."
                )
        sso = _apply_sso(db, org, idp_metadata_url)

        result = _generate(user, org, company_name, capture_pdf, db)
        result.update({
            "user_id": str(user.id),
            "user_email": user.email,
            "org_id": str(org.id),
            "org_slug": org.slug,
            "company_name": company_name,
            "subsidiaries": subsidiaries,
            "white_label": white_label,
            "sso": sso,
            "mock_idp_dir": str(mock_idp.dir) if mock_idp else None,
            "provisioning_status": {
                "multi_subsidiary": "Active" if subsidiaries else "Ready",
                "white_label": "Active" if white_label["applied"] else "Ready",
                "sso": "Active" if sso["is_active"] else "Ready",
                "notarizations": "Active",
            },
            "group_rollup_url": "/vendor/trm/subsidiary-comparison",
        })
        if not capture_pdf and not result.get("download_url"):
            result["download_url"] = latest_baseline_url(str(user.id), db)
        return result
    finally:
        if owns_db:
            db.close()


def run_sso_roundtrip(
    *, user_id: str, email: Optional[str] = None, tamper: bool = False, db=None
) -> dict[str, Any]:
    """POST a signed SAML assertion at the tenant's **real** ACS route.

    This is the difference between "the SSO page renders" and "SSO works": the
    assertion goes through `saml_service.parse_assertion` (signature, audience
    and destination checks) and then the route's JIT provisioning and token
    minting, exactly as a live IdP's POST would.

    `tamper=True` is the negative control — it must be rejected, otherwise the
    positive result only shows that *some* XML was accepted.

    Returns `{ok, assertion_valid, name_id, status_code, error?}`.
    """
    owns_db = db is None
    db = db or SessionLocal()
    idp = None
    try:
        from app.services.saml_mock_idp import MockIdp, xmlsec1_available
        from app.services.saml_service import sp_acs_url, sp_entity_id

        if not xmlsec1_available():
            return {
                "ok": False, "assertion_valid": False, "name_id": None,
                "status_code": None,
                "error": "xmlsec1 is not installed — cannot sign a test assertion. "
                         "This is a harness limitation, not an SSO failure.",
            }

        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise ValueError(f"No such user: {user_id}")
        org = (
            db.query(Organisation)
            .filter(Organisation.owner_user_id == user.id)
            .order_by(Organisation.created_at.asc())
            .first()
        )
        if not org:
            raise ValueError("Tenant has no organisation — run activate_pro_features first")

        cfg = db.query(SsoConfig).filter(SsoConfig.organisation_id == org.id).first()
        if not cfg or not cfg.is_active:
            raise ValueError("SSO is not active for this organisation")

        # Mint a fresh IdP and repoint the config at it: the metadata the previous
        # run wrote lives in a temp dir that may already be gone.
        idp = MockIdp()
        cfg.idp_metadata_url = idp.metadata_url
        db.commit()

        subject = email or f"sso.demo+{uuid.uuid4().hex[:6]}@{org.slug}.demo"
        assertion = idp.build_signed_response(
            acs_url=sp_acs_url(org.slug),
            sp_entity_id=sp_entity_id(org.slug),
            email=subject,
            tamper=tamper,
        )

        from fastapi.testclient import TestClient
        from app.main import app

        path = f"/api/v1/enterprise/sso/saml/acs/{org.slug}"
        payload = {"SAMLResponse": assertion, "RelayState": "/vendor/trm"}
        with TestClient(app) as client:
            # The redirect-suppression kwarg moved between starlette versions
            # (`allow_redirects` → `follow_redirects`); we must not follow it,
            # because the 302 *is* the success signal.
            try:
                resp = client.post(path, data=payload, follow_redirects=False)
            except TypeError:
                resp = client.post(path, data=payload, allow_redirects=False)

        # 302 to the frontend callback with tokens in the fragment = the SP
        # accepted the assertion and minted a session.
        ok = resp.status_code == 302
        location = resp.headers.get("location", "")
        return {
            "ok": ok,
            "assertion_valid": ok and "access_token=" in location,
            "name_id": subject if ok else None,
            "status_code": resp.status_code,
            "tampered": tamper,
            "error": None if ok else (resp.json().get("detail")
                                      if resp.headers.get("content-type", "").startswith("application/json")
                                      else resp.text[:300]),
        }
    finally:
        if idp:
            idp.cleanup()
        if owns_db:
            db.close()
