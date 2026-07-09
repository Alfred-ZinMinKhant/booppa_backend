"""Vendor Active insight helpers — Trust Score breakdown (4b) + sector rank (4e).

Covers `get_trust_breakdown` / `get_sector_rank` and their rendering in the
status-snapshot PDF.
"""
from io import BytesIO

from pypdf import PdfReader


def _mk_vendor(test_db, email, sector=None, scores=None):
    from app.core.models import User
    from app.core.models import VendorScore, VendorSector

    user = User(email=email, hashed_password="x", role="VENDOR",
                plan="vendor_active", company="Co " + email)
    test_db.add(user)
    test_db.commit()
    test_db.refresh(user)
    if scores is not None:
        test_db.add(VendorScore(vendor_id=user.id, **scores))
    if sector:
        test_db.add(VendorSector(vendor_id=user.id, sector=sector))
    test_db.commit()
    return user


def test_get_trust_breakdown_math(test_db):
    from app.services.vendor_active_insights import get_trust_breakdown

    user = _mk_vendor(test_db, "vp+bd@booppa.io", scores={
        "compliance_score": 100, "visibility_score": 50,
        "engagement_score": 0, "procurement_interest_score": 0,
        "total_score": 55,
    })
    bd = get_trust_breakdown(test_db, str(user.id))
    assert bd is not None
    dims = {d["label"]: d for d in bd["dimensions"]}
    assert len(dims) == 4
    # weight * (100 - score): Compliance 0.30*0=0, Visibility 0.20*50=10,
    # Engagement 0.20*100=20, Procurement 0.15*100=15.
    assert dims["Compliance"]["potential_points"] == 0
    assert dims["Visibility"]["potential_points"] == 10
    assert dims["Engagement"]["potential_points"] == 20
    assert dims["Procurement"]["potential_points"] == 15
    # top_actions sorted desc by impact, capped at 3, zero-impact dropped.
    pts = [a["potential_points"] for a in bd["top_actions"]]
    assert pts == [20, 15, 10]
    assert bd["projected_total"] == 55 + 45  # 100
    assert bd["projected_total"] >= bd["total"]


def test_get_trust_breakdown_none_without_score(test_db):
    from app.services.vendor_active_insights import get_trust_breakdown

    user = _mk_vendor(test_db, "vp+noscore@booppa.io")
    assert get_trust_breakdown(test_db, str(user.id)) is None


def test_get_sector_rank(test_db):
    from app.services.vendor_active_insights import get_sector_rank

    _mk_vendor(test_db, "vp+r1@booppa.io", sector="IT", scores={"total_score": 90})
    mid = _mk_vendor(test_db, "vp+r2@booppa.io", sector="IT", scores={"total_score": 70})
    _mk_vendor(test_db, "vp+r3@booppa.io", sector="IT", scores={"total_score": 50})

    rank = get_sector_rank(test_db, str(mid.id))
    assert rank == {"sector": "IT", "rank": 2, "total": 3}


def test_get_sector_rank_none_without_sector(test_db):
    from app.services.vendor_active_insights import get_sector_rank

    user = _mk_vendor(test_db, "vp+nosector@booppa.io", scores={"total_score": 80})
    assert get_sector_rank(test_db, str(user.id)) is None


def test_snapshot_renders_breakdown_and_rank():
    from app.services.vendor_snapshot_generator import generate_vendor_snapshot_pdf

    pdf = generate_vendor_snapshot_pdf({
        "company_name": "Smith & Jones <Holdings>",  # exercises _xml_escape
        "plan_label": "Vendor Active",
        "trust_score": 55,
        "compliance_score": 100,
        "profile_views_30d": 0,
        "trust_breakdown": {
            "total": 55,
            "projected_total": 100,
            "dimensions": [
                {"label": "Compliance", "score": 100, "action": "Done", "potential_points": 0},
                {"label": "Visibility", "score": 50, "action": "Add logo & description", "potential_points": 10},
                {"label": "Engagement", "score": 0, "action": "Submit a bid", "potential_points": 20},
                {"label": "Procurement", "score": 0, "action": "Complete PDPA Snapshot", "potential_points": 15},
            ],
            "top_actions": [
                {"label": "Engagement", "score": 0, "action": "Submit a bid", "potential_points": 20},
                {"label": "Procurement", "score": 0, "action": "Complete PDPA Snapshot", "potential_points": 15},
                {"label": "Visibility", "score": 50, "action": "Add logo & description", "potential_points": 10},
            ],
        },
        "sector_rank": {"sector": "IT", "rank": 12, "total": 847},
    })
    assert pdf.startswith(b"%PDF")
    text = "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf)).pages)
    assert "Trust Score Breakdown" in text
    assert "reaches" in text and "100/100" in text
    assert "#12" in text and "847" in text


def test_snapshot_omits_sections_when_absent():
    from app.services.vendor_snapshot_generator import generate_vendor_snapshot_pdf

    pdf = generate_vendor_snapshot_pdf({"company_name": "X", "plan_label": "Vendor Active"})
    assert pdf.startswith(b"%PDF")  # no breakdown/rank data → no crash
    text = "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf)).pages)
    assert "Trust Score Breakdown" not in text
