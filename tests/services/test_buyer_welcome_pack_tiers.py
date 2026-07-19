"""
Tier-correctness guard for the Buyer Welcome Pack + supplier certificate.

Gianpaolo's acceptance bar: every number, feature bullet, and plan-comparison
sentence in a generated document must match what *that specific* customer pays
for — not what a template originally written for another tier happens to say.

These tests pin that bar against the structured sources of truth
(app.billing.enforcement + app.core.models) so a future template edit can't
silently regress the Welcome Pack back to Essentials-only copy, or resurface the
wrong-plan certificate upsell for a Pro/Enterprise buyer.
"""
from io import BytesIO

from pypdf import PdfReader

from app.billing.enforcement import scan_limit_for, max_seats_for
from app.core.models import ENTERPRISE_NOTARIZATION_LIMITS
from app.services.buyer_essentials_pack_generator import generate_buyer_essentials_pack
from app.services.supplier_due_diligence_generator import generate_certificate_pdf


def _pdf_text(pdf_bytes: bytes) -> str:
    return "".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf_bytes)).pages)


TIERS = [
    ("buyer_starter_monthly", "Buyer Essentials", "starter"),
    ("buyer_pro_monthly", "Buyer Professional", "pro"),
    ("buyer_enterprise_monthly", "Buyer Enterprise", "enterprise"),
]


def _render(pk: str, label: str, tier: str) -> str:
    pdf = generate_buyer_essentials_pack({
        "company": "Acme Pte Ltd",
        "buyer_email": "buyer@acme.co",
        "plan_label": label,
        "product_type": pk,
        "tier": tier,
    })
    return _pdf_text(pdf)


def test_welcome_pack_header_matches_tier():
    """Header band must name the buyer's own plan, never a hardcoded tier."""
    for pk, label, tier in TIERS:
        txt = _render(pk, label, tier)
        assert label.upper() in txt.upper()
        if tier != "starter":
            # The old bug: Enterprise/Pro packs still said "BUYER ESSENTIALS".
            assert "BUYER ESSENTIALS" not in txt.upper()


def test_welcome_pack_numbers_match_enforcement():
    """Quotas rendered must equal the enforcement source of truth for the plan."""
    for pk, label, tier in TIERS:
        txt = _render(pk, label, tier)
        quick = scan_limit_for(pk, "QUICK")
        assert f"{quick} per month" in txt

        seats = max_seats_for(pk)
        assert ("Unlimited" if seats is None else str(seats)) in txt

        notar = ENTERPRISE_NOTARIZATION_LIMITS.get(pk, 1)
        assert f"{notar} per month" in txt


def test_welcome_pack_higher_tier_features_gated():
    """Deep/Enhanced/Evidence + enterprise controls appear only for their tiers."""
    starter = _render(*TIERS[0])
    pro = _render(*TIERS[1])
    ent = _render(*TIERS[2])

    # Starter: none of the premium capabilities.
    for marker in ("Deep Scans", "Enhanced Scans", "Evidence Scans", "RBAC", "RESTful API"):
        assert marker not in starter, marker

    # Pro: Deep Scans + comparison engine, but not Enterprise-only controls.
    assert "Deep Scans" in pro
    assert "comparison" in pro.lower()
    assert "Enhanced Scans" not in pro
    assert "Evidence Scans" not in pro
    assert "RBAC" not in pro

    # Enterprise: Enhanced + Evidence scans, RBAC, API.
    assert "Enhanced Scans" in ent
    assert "Evidence Scans" in ent
    assert "RBAC" in ent
    assert "RESTful API" in ent


def _cert_text(tier):
    data = {
        "supplier_name": "Vendor X Pte",
        "buyer_company": "Acme Pte Ltd",
        "resolved": True,
        "is_certificate": False,
        "anchored": False,
        "tx_hash": None,
        "buyer_tier": tier,
    }
    return _pdf_text(generate_certificate_pdf(data))


def test_certificate_upsell_only_for_starter():
    """A Pro/Enterprise buyer must not be told anchoring is 'available on Pro
    and Enterprise plans' — they already pay for it."""
    starter = _cert_text("starter")
    assert "available on Pro and" in starter

    for tier in ("pro", "enterprise"):
        txt = _cert_text(tier)
        assert "available on Pro and" not in txt
        assert "completes verification" in txt
