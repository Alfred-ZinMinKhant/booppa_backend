"""Score provenance (layer-3 backlog L3-1 / L3-2).

A vendor score is only defensible if the buyer can see the signal behind each
dimension and whether that signal was inferred from public disclosure or backed
by evidence that was actually tested. These pin both halves.
"""
import uuid
from datetime import datetime, timezone

import pytest

from tests._test_helpers import make_user


def _seed_scan(db, vendor_id, scan_id=None):
    from app.core.models import DeepScanDimensionHistory

    scan_id = scan_id or uuid.uuid4()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    dims = [
        ("pdpa", "DPO Designated (§11(3))", "Compliant", 92, {"dpo_mentioned": True}),
        ("pdpa", "Protection Obligation (§24)", "Partial", 60,
         {"ssl_grade": "B", "high_findings": 1}),
        ("pdpa", "Data Breach Notification (§26A-D)", "Compliant", 90,
         {"pdpc_enforcement_found": False, "breach_policy_mentioned": True}),
    ]
    for category, name, status, score, detail in dims:
        db.add(DeepScanDimensionHistory(
            vendor_id=vendor_id, scan_id=scan_id, category=category,
            dimension_name=name, status=status, score=score,
            detail=detail, captured_at=now,
        ))
    db.commit()
    return scan_id


def _seed_tested_evidence(db, user, domain, tested_at):
    """An organisation the vendor owns, one control in `domain`, tested evidence."""
    from app.core.models import Organisation, TrmControl, TrmEvidence

    org = Organisation(
        id=uuid.uuid4(), name="Test Org", slug=f"org-{uuid.uuid4().hex[:8]}",
        owner_user_id=user.id,
    )
    db.add(org)
    db.flush()
    control = TrmControl(id=uuid.uuid4(), organisation_id=org.id, domain=domain,
                         control_ref="TRM-1.1", status="compliant")
    db.add(control)
    db.flush()
    db.add(TrmEvidence(
        id=uuid.uuid4(), control_id=control.id, file_name="dr-test.pdf",
        evidence_type="tested", tested_at=tested_at,
        attestation="Annual DR failover test",
    ))
    db.commit()
    return org


def test_score_basis_renders_signal_and_defaults_to_inferred(test_db):
    from app.services.score_basis import BASIS_INFERRED, build_score_basis

    user = make_user(test_db, company="novapay.io")
    _seed_scan(test_db, user.id)

    rows = build_score_basis(test_db, user.id)
    assert len(rows) == 3
    by_name = {r["dimension_name"]: r for r in rows}

    # Every dimension states its driving signal in plain English, not a raw dict.
    dpo = by_name["DPO Designated (§11(3))"]
    assert dpo["signal"] == "DPO named on public site"
    assert dpo["score"] == 92
    assert "{" not in dpo["signal"]

    prot = by_name["Protection Obligation (§24)"]
    assert "TLS grade B" in prot["signal"]
    assert "1 high/critical scan finding(s)" in prot["signal"]

    # With no tested evidence on file, every row is honestly marked inferred.
    assert all(r["basis"] == BASIS_INFERRED for r in rows)


def test_tested_evidence_annotates_only_the_mapped_dimension(test_db):
    from app.services.score_basis import BASIS_INFERRED, build_score_basis

    user = make_user(test_db, company="novapay.io")
    _seed_scan(test_db, user.id)
    _seed_tested_evidence(test_db, user, "Incident Management", datetime(2026, 3, 15))

    rows = {r["dimension_name"]: r for r in build_score_basis(test_db, user.id)}

    breach = rows["Data Breach Notification (§26A-D)"]
    assert breach["basis"] == "Tested — 15 Mar 2026"
    assert breach["trm_domain"] == "Incident Management"
    # Annotation only — tested evidence must not move the number in this pass.
    assert breach["score"] == 90

    # Unrelated domains stay inferred; evidence must not bleed across dimensions.
    assert rows["DPO Designated (§11(3))"]["basis"] == BASIS_INFERRED
    assert rows["Protection Obligation (§24)"]["basis"] == BASIS_INFERRED


def test_no_scan_yields_no_section(test_db):
    from app.services.score_basis import build_score_basis

    user = make_user(test_db, company="novapay.io")
    assert build_score_basis(test_db, user.id) == []


def test_score_basis_table_renders_in_vendor_pro_report():
    """The section must reach the PDF, citation and basis intact."""
    from io import BytesIO

    from pypdf import PdfReader

    from app.services.vendor_pro_report_generator import generate_vendor_pro_report_pdf

    pdf = generate_vendor_pro_report_pdf({
        "company_name": "NOVAPAY PTE. LTD.",
        "score_basis": [
            {"dimension_name": "DPO Designated (§11(3))", "status": "Compliant",
             "score": 92, "signal": "DPO named on public site",
             "basis": "Inferred (public scan)"},
            {"dimension_name": "Data Breach Notification (§26A-D)", "status": "Compliant",
             "score": 90, "signal": "Breach-response policy published",
             "basis": "Tested — 15 Mar 2026"},
        ],
    })
    raw = "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf)).pages)
    # Table cells wrap, so compare against whitespace-collapsed text.
    text = " ".join(raw.split())
    assert "Score basis" in text
    assert "§11(3)" in text
    assert "Inferred (public scan)" in text
    assert "Tested — 15 Mar 2026" in text
