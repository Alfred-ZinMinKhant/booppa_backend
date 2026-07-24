"""The CSP onboarding demo harness must produce real inspection output.

Gianpaolo's ask was not "describe the records" — it was "complete the onboarding
flow on a test org and send me the actual fit-and-proper writeup and an STR
rationale." These tests pin that the harness produces:

  * a nominee director with a `fit_proper` determination and a substantive,
    CSP-authored outcome + reasoning (not filler),
  * an STR decision (not filed) with a substantive rationale,
  * both records rendered to real PDFs carrying the entity's legal name and the
    CSP's own reasoning verbatim,

and that a second run does not accumulate duplicate client/nominee/STR rows.
"""
import pytest

from app.services import csp_demo_harness
from app.services.csp_demo_harness import (
    NOMINEE_NOTES,
    NOMINEE_OUTCOME,
    STR_RATIONALE,
    run_csp_onboarding_demo,
)


@pytest.fixture(autouse=True)
def _no_acra(monkeypatch):
    """Keep the baseline's ACRA lookup offline and deterministic."""
    async def _fake(*a, **k):
        return {}
    monkeypatch.setattr(
        "app.services.evidence_enricher.fetch_acra_status", _fake
    )


def _pdf_text(pdf_bytes: bytes) -> str:
    from io import BytesIO

    from pypdf import PdfReader

    reader = PdfReader(BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def test_harness_produces_a_fit_proper_nominee_with_real_reasoning(test_db):
    result = run_csp_onboarding_demo(capture_pdfs=True, db=test_db)

    nominee = result["nominee"]
    assert nominee["determination"] == "fit_proper"
    # Substantive, not a one-word outcome (what the schema rejects).
    assert nominee["outcome"] == NOMINEE_OUTCOME
    assert len(nominee["outcome"]) > 20
    assert nominee["pdf_bytes"].startswith(b"%PDF")
    assert len(nominee["record_sha256"]) == 64


def test_harness_produces_a_not_filed_str_with_a_rationale(test_db):
    result = run_csp_onboarding_demo(capture_pdfs=True, db=test_db)

    rep = result["str"]
    assert rep["decision"] == "not_filed"
    assert rep["rationale"] == STR_RATIONALE
    assert len(rep["rationale"]) > 20
    assert rep["pdf_bytes"].startswith(b"%PDF")


def test_records_render_with_the_entity_and_the_csp_reasoning_verbatim(test_db):
    result = run_csp_onboarding_demo(capture_pdfs=True, db=test_db)
    entity = result["entity"]["legal_name"]

    nominee_text = _pdf_text(result["nominee"]["pdf_bytes"])
    assert entity in nominee_text
    assert "Tan Wei Ling" in nominee_text
    # The assessor's own reasoning must survive to the rendered page.
    assert NOMINEE_NOTES[:40] in nominee_text

    str_text = _pdf_text(result["str"]["pdf_bytes"])
    assert entity in str_text
    assert STR_RATIONALE[:40] in str_text
    # A decision not to file must be framed as a decision, not a blank.
    assert "decision not to file" in str_text.lower()


def test_baseline_is_produced_for_the_customer_entity(test_db):
    result = run_csp_onboarding_demo(capture_pdfs=True, db=test_db)

    assert result["baseline"]["pdf_bytes"].startswith(b"%PDF")
    text = _pdf_text(result["baseline"]["pdf_bytes"])
    assert result["entity"]["legal_name"] in text
    # The buyer's own entity is the assessed entity — never Booppa.
    assert "Registration Readiness Baseline" in text


def test_rerun_does_not_accumulate_duplicate_records(test_db):
    from app.core.models import CspClient, CspNomineeDirector, CspProfile, CspStrReport

    first = run_csp_onboarding_demo(capture_pdfs=True, db=test_db)
    second = run_csp_onboarding_demo(capture_pdfs=True, db=test_db)

    # Same tenant + profile reused, not forked.
    assert first["user_id"] == second["user_id"]
    assert first["profile_id"] == second["profile_id"]

    profile_id = first["profile_id"]
    assert test_db.query(CspProfile).filter(
        CspProfile.id == profile_id).count() == 1
    assert test_db.query(CspClient).filter(
        CspClient.csp_id == profile_id).count() == 1
    assert test_db.query(CspNomineeDirector).filter(
        CspNomineeDirector.csp_id == profile_id).count() == 1
    assert test_db.query(CspStrReport).filter(
        CspStrReport.csp_id == profile_id).count() == 1
