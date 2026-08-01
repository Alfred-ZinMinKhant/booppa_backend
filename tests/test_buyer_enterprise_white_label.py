"""Buyer Enterprise's scoped white-label on its own deliverables.

Buyer Enterprise (SGD 799/mo) advertises "White-label reports" in its activation
email with a live CTA. That promise was previously unreachable twice over: the
PUT /white-label gate 402'd Buyer Enterprise, and no buyer-facing generator read
a WhiteLabelConfig at all, so opening the gate alone would have produced a
settings page nothing consumed.

Scope boundary these tests pin:
  * Buyer Enterprise  → branding on Procurement Report + Welcome Pack.
  * Buyer Pro / below → Booppa branding (no entitlement).
  * MAS TRM board report → Pro Suite only (covered in test_pro_suite_white_label).
"""
import uuid

from app.billing.enforcement import BUYER_WHITE_LABEL_PLAN_KEYS
from app.core.models import WhiteLabelConfig
from app.services.buyer_essentials_pack_generator import generate_buyer_essentials_pack
from app.services.buyer_procurement_report_generator import (
    generate_buyer_procurement_report_pdf,
)
from app.white_label_and_sso import resolve_branding_for_owner

from tests._test_helpers import make_user, make_org

# An 8x8 white PNG. Deliberately a FULLY DECODABLE one: the 1x1 fixture used
# elsewhere in the suite has a truncated IDAT, so PIL raises "broken data stream"
# on .load(). That is invisible in tests whose code path swallows logo errors,
# but it would mask a real regression here — these tests assert the logo is
# actually rendered, not merely accepted.
PNG_8X8 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000080000000808020000004b6d29dc"
    "0000000f49444154789c63f88f03300c2d0900ba1ebf4130930afc0000000049454e44ae426082"
)

BRANDING = {
    "logo_bytes": PNG_8X8,
    "primary_color": "#ff0000",
    "secondary_color": "#00ff00",
    "footer_text": "Acme Procurement Pte Ltd — Confidential",
    "report_header_text": "Acme Procurement",
}


def _procurement_pdf(white_label=None) -> bytes:
    return generate_buyer_procurement_report_pdf({
        "company_name": "Acme Procurement Pte Ltd",
        "plan_label": "Buyer Enterprise",
        "tier": "enterprise",
        "white_label": white_label,
        "watchlist_summary": {"total": 0, "alerting": 0, "slipped": 0, "alerting_names": []},
        "watched_suppliers": [],
        "tender_matches": [],
    })


def _welcome_pack(white_label=None) -> bytes:
    return generate_buyer_essentials_pack({
        "company": "Acme Procurement Pte Ltd",
        "buyer_email": "ops@acme.example",
        "plan_label": "Buyer Enterprise",
        "product_type": "buyer_enterprise_monthly",
        "tier": "enterprise",
        "white_label": white_label,
    })


# ── Renderers honour branding ────────────────────────────────────────────────


def test_procurement_report_applies_branding():
    branded = _procurement_pdf(BRANDING)
    assert branded.startswith(b"%PDF")
    # Must not silently ignore the branding and fall back to the identical
    # default Booppa asset — that was the pre-fix failure mode.
    assert branded != _procurement_pdf(None)


def test_welcome_pack_applies_branding():
    branded = _welcome_pack(BRANDING)
    assert branded.startswith(b"%PDF")
    assert branded != _welcome_pack(None)


def test_branded_pdfs_keep_all_their_content(caplog):
    """A `branded != default` assertion alone is too weak: it still passes when
    branding silently drops the logo (or a whole flowable) via a swallowed
    try/except. Pin page-count parity and an empty warning log instead — the
    cover-logo path raised "expected str, bytes or os.PathLike" this way.
    """
    from pypdf import PdfReader
    from io import BytesIO as _B

    with caplog.at_level("WARNING"):
        branded_pack = _welcome_pack(BRANDING)
        branded_report = _procurement_pdf(BRANDING)

    assert "render failed" not in caplog.text, caplog.text
    assert len(PdfReader(_B(branded_pack)).pages) == len(PdfReader(_B(_welcome_pack(None))).pages)
    assert len(PdfReader(_B(branded_report)).pages) == len(PdfReader(_B(_procurement_pdf(None))).pages)


def test_buyer_pdfs_render_without_branding():
    """No entitlement / no config is the common path and must still render."""
    assert _procurement_pdf(None).startswith(b"%PDF")
    assert _welcome_pack(None).startswith(b"%PDF")


def test_branding_survives_a_corrupt_logo():
    """A bad logo must degrade to the Booppa wordmark, never break generation."""
    bad = dict(BRANDING, logo_bytes=b"not-a-png")
    assert _procurement_pdf(bad).startswith(b"%PDF")
    assert _welcome_pack(bad).startswith(b"%PDF")


# ── The pack documents what the activation email advertises ──────────────────


def _pack_text(product_type, tier):
    from io import BytesIO as _B

    from pypdf import PdfReader

    pdf = generate_buyer_essentials_pack({
        "company": "Acme Procurement Pte Ltd",
        "buyer_email": "ops@acme.example",
        "plan_label": "Buyer Enterprise" if tier == "enterprise" else "Buyer Essentials",
        "product_type": product_type,
        "tier": tier,
    })
    return "\n".join(p.extract_text() or "" for p in PdfReader(_B(pdf)).pages)


def test_enterprise_pack_documents_subsidiaries_and_white_label():
    """Gianpaolo's original finding: the activation email advertises both with
    live CTAs, but the Welcome Pack — the document meant to walk through
    everything the plan includes — never mentioned either.
    """
    text = _pack_text("buyer_enterprise_monthly", "enterprise")
    assert "Multi-subsidiary management" in text
    assert "White-label reports" in text


def test_starter_pack_does_not_promise_enterprise_features():
    """The flip side: the pack must not document what Starter's gates would 402."""
    text = _pack_text("buyer_starter_monthly", "starter")
    assert "White-label reports" not in text
    assert "Multi-subsidiary management" not in text


# ── Entitlement resolution ───────────────────────────────────────────────────


def _config_org(test_db, plan):
    owner = make_user(test_db, plan=plan, role="PROCUREMENT")
    org = make_org(test_db, owner=owner)
    test_db.add(WhiteLabelConfig(
        id=uuid.uuid4(),
        organisation_id=org.id,
        primary_color="#ff0000",
        secondary_color="#00ff00",
        footer_text="Acme — Confidential",
        report_header_text="Acme Procurement",
    ))
    test_db.commit()
    return owner, org


def test_resolver_returns_branding_for_buyer_enterprise(test_db):
    owner, _ = _config_org(test_db, "buyer_enterprise_monthly")
    branding = resolve_branding_for_owner(
        owner.id, test_db, plan_keys=BUYER_WHITE_LABEL_PLAN_KEYS,
    )
    assert branding is not None
    # Mapping is deliberate: the customer's primary colour becomes the header
    # band, which pdf_layout.draw_page reads as `secondary_color`.
    assert branding["secondary_color"] == "#ff0000"
    assert branding["primary_color"] == "#00ff00"
    assert branding["report_header_text"] == "Acme Procurement"


def test_resolver_denies_buyer_pro_even_with_a_config(test_db):
    """A downgraded buyer's next cycle reverts to Booppa branding."""
    owner, _ = _config_org(test_db, "buyer_pro_monthly")
    assert resolve_branding_for_owner(
        owner.id, test_db, plan_keys=BUYER_WHITE_LABEL_PLAN_KEYS,
    ) is None


def test_resolver_returns_none_without_a_config(test_db):
    owner = make_user(test_db, plan="buyer_enterprise_monthly", role="PROCUREMENT")
    make_org(test_db, owner=owner)
    assert resolve_branding_for_owner(
        owner.id, test_db, plan_keys=BUYER_WHITE_LABEL_PLAN_KEYS,
    ) is None
