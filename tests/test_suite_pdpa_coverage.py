"""PDPA coverage for Standard/Pro Suite.

The Suite shipped assessing MAS TRM only. MAS TRM and the PDPA are separate
regimes with separate regulators, and a MAS-regulated FI is fully subject to
both — so a Suite board report that showed only TRM implied a completeness it
did not have.

Two of the defects pinned here were latent `NameError`s swallowed by an
enclosing `except Exception`, which is the failure mode these tests exist to
stop recurring: nothing errored visibly, the work simply never ran.
"""
from __future__ import annotations

import inspect

import pytest


# ── The constant that silently killed the board report ───────────────────────

def test_board_report_schema_version_is_importable():
    """`run_trm_board_report_for_user` imports this at the top of its body.

    It was never defined, so the import raised ImportError on every run, the
    task's generic `except Exception` caught it, and it retried twice and died.
    No Suite subscriber has ever received a monthly board report and nothing
    surfaced the failure.
    """
    from app.services.trm_board_report_generator import (
        TRM_BOARD_REPORT_SCHEMA_VERSION,
    )

    assert isinstance(TRM_BOARD_REPORT_SCHEMA_VERSION, int)
    assert TRM_BOARD_REPORT_SCHEMA_VERSION >= 2


# ── The unbound names in the activation path ─────────────────────────────────

def test_activation_accepts_metadata_and_session_id():
    """Both were read by the first-cycle block but were never parameters.

    The visible damage: Tender Intelligence's branch aborted at its first
    `metadata.get(...)` (no welcome digest), and Suite activations never seeded
    `mas_licence_type` from checkout.
    """
    from app.services.fulfillment.subscriptions import _activate_subscription

    params = inspect.signature(_activate_subscription).parameters
    assert "metadata" in params
    assert "session_id" in params
    # Defaulted, so a caller that genuinely has neither stays correct.
    assert params["metadata"].default is None
    assert params["session_id"].default is None


def test_webhook_passes_metadata_and_session_id_through():
    import app.api.stripe_webhook as wh

    src = inspect.getsource(wh)
    assert "metadata=session.get(\"metadata\")" in src
    assert "session_id=session.get(\"id\")" in src


# ── The board report's PDPA section ──────────────────────────────────────────

def _render(pdpa):
    from app.services.trm_board_report_generator import generate_trm_board_report_pdf

    return generate_trm_board_report_pdf({
        "company_name": "Acme Pte Ltd",
        "plan_label": "Standard Suite",
        "domains": [{"domain": "Governance", "status": "compliant", "risk_rating": "high"}],
        "compliant_pct": 40,
        "previous_pct": None,
        "pdpa": pdpa,
    })


def _text(pdf_bytes: bytes) -> str:
    """Extracted page text, whitespace-normalised.

    ReportLab wraps Paragraph text at arbitrary points, so an assertion on a
    phrase that happens to straddle a line break fails for reasons that have
    nothing to do with the content. Collapsing runs of whitespace makes the
    assertions test what the reader sees rather than where the layout broke.
    """
    from io import BytesIO

    from pypdf import PdfReader

    raw = "\n".join(
        page.extract_text() or "" for page in PdfReader(BytesIO(pdf_bytes)).pages
    )
    return " ".join(raw.split())


@pytest.mark.parametrize("missing", [None, {}, {"assessed": False, "score": None}])
def test_absent_scan_renders_not_assessed_never_compliant(missing):
    """The absence-of-evidence rule, on the one document a board reads.

    A missing scan is the absence of an assessment, not a finding of compliance.
    """
    body = _text(_render(missing))
    assert "Not Assessed" in body
    # The section must not assert a PDPA position it does not hold.
    assert "posture score" not in body


def test_scored_scan_renders_the_score_and_band():
    body = _text(_render({
        "assessed": True, "score": 84, "website": "acme.sg",
        "scanned_at": "01 August 2026", "evidence_pack_status": "ready",
    }))
    assert "84/100" in body
    assert "GREEN" in body
    assert "acme.sg" in body


def test_issued_evidence_pack_does_not_claim_the_controls_were_tested():
    """Documented is not tested — the layer-3 trap.

    A regulator treats an untested plan as an aspiration. The pack line may say
    the documents exist; it must not imply the controls behind them were
    exercised, because we hold no evidence of that.
    """
    body = _text(_render({
        "assessed": True, "score": 90, "evidence_pack_status": "ready",
    }))
    low = body.lower()
    # The distinction must be stated, not merely implied by omission.
    assert "they are not evidence that it has been tested" in low
    # ...and it must ask for the missing thing rather than leaving it as a note.
    assert "test of the breach-response plan" in low
    # No unqualified compliance claim anywhere in the document.
    assert "pdpa compliant" not in low


def test_outstanding_evidence_pack_says_no_documentation_exists():
    body = _text(_render({
        "assessed": True, "score": 70, "evidence_pack_status": "intake_pending",
    }))
    assert "outstanding" in body.lower()


# ── Activation wiring ────────────────────────────────────────────────────────

def test_suite_activation_queues_the_pdpa_scan_and_evidence_pack():
    from app.services.fulfillment import subscriptions

    src = inspect.getsource(subscriptions._activate_subscription)
    suite_branch = src.split('elif new_plan in ("standard_suite", "pro_suite")')[1]
    assert "run_suite_pdpa_scan_for_user" in suite_branch
    assert "_fulfill_compliance_evidence_pack" in suite_branch


def test_suite_pdpa_scan_skips_an_account_with_no_website(test_db, monkeypatch):
    """Nothing to scan. Inventing a domain would score someone else's site."""
    from app.workers import tasks as t
    from tests._test_helpers import make_user

    user = make_user(test_db, plan="standard_suite")
    user.website = None
    test_db.commit()

    called = {}
    monkeypatch.setattr(t, "SessionLocal", lambda: test_db)
    monkeypatch.setattr(
        "app.services.fulfillment.single_products._fulfill_pdpa",
        lambda *a, **k: called.setdefault("ran", True),
    )

    t.run_suite_pdpa_scan_for_user.run(str(user.id))
    assert "ran" not in called


def test_suite_pdpa_scan_creates_a_quick_scan_report(test_db, monkeypatch):
    """The row must be an ordinary `pdpa_quick_scan` Report.

    That is what makes the board report's scoped lookup pick it up unchanged —
    no Suite-specific framework the PDPA readers would not recognise.
    """
    from app.core.models import Report
    from app.workers import tasks as t
    from tests._test_helpers import make_user

    user = make_user(test_db, plan="standard_suite")
    user.website = "https://acme.sg"
    test_db.commit()

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(t, "SessionLocal", lambda: test_db)
    monkeypatch.setattr("app.services.fulfillment._fulfill_pdpa", _noop, raising=False)
    monkeypatch.setattr(
        "app.services.fulfillment.single_products._fulfill_pdpa", _noop
    )

    t.run_suite_pdpa_scan_for_user.run(str(user.id))

    row = (
        test_db.query(Report)
        .filter(Report.owner_id == user.id, Report.framework == "pdpa_quick_scan")
        .first()
    )
    assert row is not None
    assert row.company_website == "https://acme.sg"
    assert row.assessment_data["triggered_by"] == "suite_activation"
