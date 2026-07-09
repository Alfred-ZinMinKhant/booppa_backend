"""MAS TRM monthly board report — data shaping + PDF render (Sprint 9d)."""
from app.core.models import MAS_TRM_DOMAINS
from app.services.trm_board_report_generator import (
    board_data_from_controls,
    generate_trm_board_report_pdf,
)


def _controls():
    # 4 compliant, 1 critical gap, rest not started.
    out = []
    for i, d in enumerate(MAS_TRM_DOMAINS):
        if i < 4:
            out.append({"domain": d, "status": "compliant", "risk_rating": "low"})
        elif i == 4:
            out.append({"domain": d, "status": "gap", "risk_rating": "critical"})
        else:
            out.append({"domain": d, "status": "not_started", "risk_rating": None})
    return out


def test_board_data_shaping():
    bd = board_data_from_controls(_controls(), "fintech")
    assert bd["compliant_pct"] == round(100 * 4 / 13)
    # Sector ordering: fintech leads with Technology Risk Governance.
    assert bd["domains"][0]["domain"] == "Technology Risk Governance"
    assert len(bd["domains"]) == 13
    # The open critical control surfaces as a top risk.
    assert any("critical" in r for r in bd["top_risks"])
    assert bd["next_focus"]  # some non-compliant domain to focus on


def test_standard_report_renders_pdf():
    bd = board_data_from_controls(_controls(), None)
    pdf = generate_trm_board_report_pdf({
        "company_name": "Acme",
        "plan_label": "Standard Suite",
        "domains": bd["domains"],
        "compliant_pct": bd["compliant_pct"],
        "previous_pct": 20,
        "top_risks": bd["top_risks"],
        "next_focus": bd["next_focus"],
    })
    assert pdf[:4] == b"%PDF"


def test_pro_white_label_report_renders_pdf():
    bd = board_data_from_controls(_controls(), "fintech")
    pdf = generate_trm_board_report_pdf({
        "company_name": "Acme",
        "plan_label": "Pro Suite",
        "domains": bd["domains"],
        "compliant_pct": bd["compliant_pct"],
        "previous_pct": None,  # first cycle → baseline
        "top_risks": bd["top_risks"],
        "next_focus": bd["next_focus"],
        "white_label": {
            "primary_color": "#123456",
            "secondary_color": "#abcdef",
            "footer_text": "Confidential — Acme",
            "report_header_text": "Acme Group Risk",
        },
    })
    assert pdf[:4] == b"%PDF"
