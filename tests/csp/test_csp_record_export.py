"""Inspection-record export for nominee fit-and-proper and STR decisions.

Two things are pinned here.

1. The input gate. Under the CSP Act 2024 the fit-and-proper assessment must be
   genuinely performed by the registered CSP. A one-word outcome with no
   reasoning, or a `fit_proper` result with the statutory checks unticked, is not
   an assessment — the schema must reject both. STR already met this bar
   (`min_length=20` on the rationale); these tests guard that it stays met.

2. The rendered record. The reasoning is written by the CSP and must appear
   verbatim, alongside which checks were performed and the on-chain anchor —
   otherwise there is nothing to hand an ACRA inspector.
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api.csp_schemas import NomineeAssessmentUpdate, StrCreate
from app.services.csp_record_export import (
    build_nominee_assessment_record,
    build_str_decision_record,
)


NOW = datetime(2026, 3, 14, tzinfo=timezone.utc)

_NOTES = (
    "Cross-checked the subject against the Singapore Courts bankruptcy search and "
    "the ACRA disqualified-directors register on 14 March 2026; both returned nil. "
    "Prior directorship at Meridian Freight Pte Ltd ended by members' voluntary "
    "winding up, not insolvency."
)
_OUTCOME = "Fit and proper — no adverse findings across all three statutory checks."


def _profile(**kw):
    base = {"id": uuid.uuid4(), "legal_name": "Acme Corporate Services Pte Ltd",
            "uen": "201912345A"}
    base.update(kw)
    return SimpleNamespace(**base)


def _nominee(**kw):
    base = {
        "id": uuid.uuid4(),
        "nominee_full_name": "Tan Wei Ling",
        "nominee_nationality": "Singaporean",
        "nominator_name": "Bright Harbour Holdings Ltd",
        "company_name": "Bright Harbour SG Pte Ltd",
        "company_uen": "202398765B",
        "appointment_date": NOW,
        "cessation_date": None,
        "is_active": True,
        "assessment_status": "fit_proper",
        "assessment_date": NOW,
        "assessed_by": "Jane Tan (RQI)",
        "criminal_check_done": True,
        "bankruptcy_check_done": True,
        "director_history_check": True,
        "assessment_outcome": _OUTCOME,
        "assessment_notes": _NOTES,
        "acra_disclosed": True,
        "acra_filing_date": NOW,
        "acra_filing_ref": "BZF-2026-0091",
        "next_review": NOW + timedelta(days=365),
        "blockchain_tx_hash": None,
        "polygonscan_url": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


_RATIONALE = (
    "The S$48,000 inflow reconciles to the invoice trail for the Batam warehouse "
    "lease already on file, and the counterparty is the client's disclosed parent. "
    "No indicator of predicate criminality; filing would not be supportable."
)


def _str_report(**kw):
    base = {
        "id": uuid.uuid4(),
        "client_id": uuid.uuid4(),
        "trigger_type": "unusual_transaction",
        "trigger_detail": "Single inbound transfer of S$48,000 from an offshore account.",
        "amount_involved": 48000.0,
        "currency": "SGD",
        "transaction_date": NOW,
        "decision": "not_filed",
        "decision_by": "John Lim (AML Compliance Officer)",
        "decision_date": NOW,
        "decision_rationale": _RATIONALE,
        "stro_reference": None,
        "stro_filed_date": None,
        "stro_filed_by": None,
        "client_notified": False,
        "service_declined": False,
        "escalated_to_senior_mgmt": False,
        "senior_mgmt_name": None,
        "escalation_date": None,
        "blockchain_tx_hash": None,
        "polygonscan_url": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _assessment_payload(**kw):
    base = {
        "assessment_outcome": _OUTCOME,
        "assessed_by": "Jane Tan (RQI)",
        "criminal_check_done": True,
        "bankruptcy_check_done": True,
        "director_history_check": True,
        "assessment_notes": _NOTES,
        "result": "fit_proper",
    }
    base.update(kw)
    return base


# ── INPUT GATE ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("notes", [None, "", "Looks fine."])
def test_assessment_without_real_reasoning_is_rejected(notes):
    """A blank or one-line note is the 'generic template' outcome ACRA does not
    accept — the reasoning is what answers an inspector."""
    with pytest.raises(ValidationError):
        NomineeAssessmentUpdate(**_assessment_payload(assessment_notes=notes))


def test_assessment_with_a_one_word_outcome_is_rejected():
    with pytest.raises(ValidationError):
        NomineeAssessmentUpdate(**_assessment_payload(assessment_outcome="Pass"))


@pytest.mark.parametrize("unticked", [
    "criminal_check_done", "bankruptcy_check_done", "director_history_check",
])
def test_fit_proper_requires_every_statutory_check_performed(unticked):
    """A pass outcome with an unperformed check is not an assessment."""
    with pytest.raises(ValidationError) as exc:
        NomineeAssessmentUpdate(**_assessment_payload(**{unticked: False}))
    assert "fit_proper" in str(exc.value)


def test_not_fit_may_be_recorded_with_checks_incomplete():
    """A failure can be determined the moment one check comes back adverse — the
    gate must not block recording it."""
    payload = NomineeAssessmentUpdate(**_assessment_payload(
        result="not_fit", criminal_check_done=False,
        assessment_outcome="Not fit and proper — undischarged bankruptcy confirmed.",
    ))
    assert payload.result == "not_fit"


def test_str_rationale_floor_still_holds():
    """Guards the gate STR already had."""
    with pytest.raises(ValidationError):
        StrCreate(
            trigger_type="unusual_transaction",
            trigger_detail="Single inbound transfer of S$48,000 offshore.",
            decision="not_filed", decision_by="John Lim",
            decision_rationale="Fine.",
        )


# ── RENDERED NOMINEE RECORD ───────────────────────────────────────────────────

def test_nominee_record_carries_the_assessors_own_reasoning_verbatim():
    _, body = build_nominee_assessment_record(_nominee(), _profile())
    assert _NOTES in body
    assert _OUTCOME in body
    assert "Jane Tan (RQI)" in body
    assert "Acme Corporate Services Pte Ltd" in body
    assert "201912345A" in body
    # Booppa must not be read as the author of the assessment.
    assert "did not author the reasoning" in body


def test_nominee_record_states_every_check_as_performed_or_not():
    """A blank cell reads as 'no answer' to an inspector; say which it is."""
    _, body = build_nominee_assessment_record(
        _nominee(result=None, bankruptcy_check_done=False,
                 assessment_status="under_review"),
        _profile(),
    )
    assert "Criminal record check:** Performed" in body
    assert "Bankruptcy check:** NOT performed" in body
    assert "Director history check:** Performed" in body


def test_nominee_record_says_anchoring_is_pending_when_it_is():
    _, body = build_nominee_assessment_record(_nominee(), _profile())
    assert "Not yet anchored" in body


def test_nominee_record_renders_the_anchor_when_notarized():
    evidence = SimpleNamespace(
        tx_hash="0xabc123", document_hash="d" * 64, network="polygon-amoy",
        block_number=987654, blockchain_timestamp=NOW,
        polygonscan_url="https://amoy.polygonscan.com/tx/0xabc123",
    )
    _, body = build_nominee_assessment_record(_nominee(), _profile(), evidence=evidence)
    assert "0xabc123" in body
    assert "amoy.polygonscan.com" in body
    assert "Not yet anchored" not in body


# ── RENDERED STR RECORD ───────────────────────────────────────────────────────

def test_not_filed_record_frames_the_decision_as_a_decision():
    """The whole point of the record: an inspector asking 'why didn't you file'
    must find a specific answer, not boilerplate."""
    title, body = build_str_decision_record(_str_report(), _profile())
    assert "A decision not to file is itself a decision" in body
    assert _RATIONALE in body
    assert "Not Filed" in title


def test_filed_record_carries_the_stro_reference_and_tipping_off_notice():
    report = _str_report(
        decision="filed", stro_reference="STRO/2026/00417",
        stro_filed_date=NOW, stro_filed_by="John Lim",
        decision_rationale="Counterparty matched an OFAC SDN entry on screening; "
                           "no supportable commercial explanation was offered.",
    )
    _, body = build_str_decision_record(report, _profile())
    assert "STRO/2026/00417" in body
    assert "48A" in body
    assert "Client notified:** No" in body
    assert "A decision not to file" not in body


def test_str_record_names_the_client_when_one_is_linked():
    client = SimpleNamespace(legal_name="Bright Harbour SG Pte Ltd")
    _, body = build_str_decision_record(_str_report(), _profile(), client=client)
    assert "Bright Harbour SG Pte Ltd" in body


# ── PDF RENDERING ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("builder,args", [
    ("nominee", None),
    ("str", None),
])
def test_records_render_to_a_real_pdf_with_an_ampersand_in_the_name(builder, args):
    """ReportLab Paragraph mini-XML treats & as an entity start — an unescaped
    name raises or renders mangled (the 'Q&A Coverage' glitch)."""
    from app.services.csp_doc_generator import generate_csp_document_pdf

    profile = _profile(legal_name="Tan & Sons Corporate Services Pte Ltd")
    if builder == "nominee":
        title, body = build_nominee_assessment_record(
            _nominee(nominator_name="Lim & Partners LLP"), profile)
    else:
        title, body = build_str_decision_record(_str_report(), profile)

    pdf_bytes, sha = generate_csp_document_pdf(
        title, body, meta={"legal_name": profile.legal_name, "uen": profile.uen})
    assert pdf_bytes.startswith(b"%PDF")
    assert len(pdf_bytes) > 3000
    assert len(sha) == 64


def test_bold_markers_become_tags_but_user_typed_markup_stays_inert():
    """The record bodies use `**label:**`, which the shared renderer had no
    parsing for — it would have printed literal asterisks. Escaping must still
    run first so a `<b>` inside the CSP's own reasoning is not treated as markup.
    """
    from app.services.csp_doc_generator import _inline

    assert _inline("**Bankruptcy check:** NOT performed") == \
        "<b>Bankruptcy check:</b> NOT performed"
    assert _inline("Tan & Sons <b>Ltd</b>") == "Tan &amp; Sons &lt;b&gt;Ltd&lt;/b&gt;"
    assert _inline("plain line") == "plain line"


# ── ENDPOINTS ─────────────────────────────────────────────────────────────────

@pytest.fixture
def stub_s3(monkeypatch):
    """Stub the S3 client the adapter builds in __init__.

    Local `.env` carries live AWS creds, so an unstubbed run writes to the real
    bucket and then 502s in CI (whose IAM user is scoped to test/* only).
    """
    from unittest.mock import MagicMock

    import app.adapters.s3_storage as s3mod

    fake = MagicMock()
    fake.put_object.return_value = {}
    fake.generate_presigned_url.return_value = "https://s3.example/signed.pdf"
    monkeypatch.setattr(s3mod.boto3, "client", lambda *a, **kw: fake)
    return fake


def _seed_csp(db, email: str):
    """Authenticated user with an ACTIVE CSP org and a profile."""
    from app.core.models import CspProfile, User
    from app.services.csp_access import activate_csp_access

    user = User(email=email, hashed_password="not-a-real-hash", role="VENDOR",
                plan="free", is_active=True)
    db.add(user)
    db.commit()
    db.refresh(user)

    org = activate_csp_access(db, user=user, plan="csp", billing_type="one_time")
    profile = CspProfile(
        organisation_id=org.id,
        legal_name="Acme Corporate Services Pte Ltd",
        uen=f"2019{uuid.uuid4().hex[:6].upper()}",
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return user, profile


def _auth_header(email: str) -> dict:
    from app.core.auth import create_access_token
    return {"Authorization": f"Bearer {create_access_token({'sub': email})}"}


def _seed_client(db, profile):
    from app.core.models import CspClient
    row = CspClient(csp_id=profile.id, client_type="company",
                    legal_name="Bright Harbour SG Pte Ltd")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_record_endpoint_409s_when_no_assessment_has_been_recorded(
    client, test_db, stub_s3,
):
    from app.core.models import CspNomineeDirector, NomineeAssessment

    user, profile = _seed_csp(test_db, "csp-unassessed@example.com")
    subject = _seed_client(test_db, profile)
    nominee = CspNomineeDirector(
        csp_id=profile.id, client_id=subject.id,
        nominee_full_name="Tan Wei Ling", nominator_name="Bright Harbour Holdings Ltd",
        assessment_status=NomineeAssessment.NOT_ASSESSED,
    )
    test_db.add(nominee)
    test_db.commit()
    test_db.refresh(nominee)

    res = client.get(f"/api/v1/csp/nominees/directors/{nominee.id}/record",
                     headers=_auth_header(user.email))
    assert res.status_code == 409
    assert "nothing to export" in res.json()["detail"]


def test_record_endpoint_serves_a_completed_assessment(client, test_db, stub_s3):
    from app.core.models import CspNomineeDirector, NomineeAssessment

    user, profile = _seed_csp(test_db, "csp-assessed@example.com")
    subject = _seed_client(test_db, profile)
    nominee = CspNomineeDirector(
        csp_id=profile.id, client_id=subject.id,
        nominee_full_name="Tan Wei Ling", nominator_name="Bright Harbour Holdings Ltd",
        assessment_status=NomineeAssessment.FIT_PROPER,
        assessment_date=NOW, assessed_by="Jane Tan (RQI)",
        criminal_check_done=True, bankruptcy_check_done=True,
        director_history_check=True,
        assessment_outcome=_OUTCOME, assessment_notes=_NOTES,
    )
    test_db.add(nominee)
    test_db.commit()
    test_db.refresh(nominee)

    res = client.get(f"/api/v1/csp/nominees/directors/{nominee.id}/record",
                     headers=_auth_header(user.email))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["download_url"] == "https://s3.example/signed.pdf"
    assert len(body["record_hash"]) == 64
    assert "Tan Wei Ling" in body["title"]
    # Rendered and stored under the caller's own profile prefix.
    key = stub_s3.put_object.call_args.kwargs["Key"]
    assert key == f"csp/{profile.id}/records/nominee-assessment-{nominee.id}.pdf"


def test_str_record_endpoint_serves_a_not_filed_decision(client, test_db, stub_s3):
    from app.core.models import CspStrReport, StrDecision

    user, profile = _seed_csp(test_db, "csp-str@example.com")
    subject = _seed_client(test_db, profile)
    report = CspStrReport(
        csp_id=profile.id, client_id=subject.id,
        trigger_type="unusual_transaction",
        trigger_detail="Single inbound transfer of S$48,000 from an offshore account.",
        decision=StrDecision.NOT_FILED, decision_by="John Lim",
        decision_date=NOW, decision_rationale=_RATIONALE, client_notified=False,
    )
    test_db.add(report)
    test_db.commit()
    test_db.refresh(report)

    res = client.get(f"/api/v1/csp/str/{report.id}/record",
                     headers=_auth_header(user.email))
    assert res.status_code == 200, res.text
    assert res.json()["decision"] == "not_filed"
    assert res.json()["download_url"] == "https://s3.example/signed.pdf"


def test_records_of_another_csp_are_not_reachable(client, test_db, stub_s3):
    """Profile-scoped lookup — one CSP must never pull another's STR record."""
    from app.core.models import CspStrReport, StrDecision

    owner, owner_profile = _seed_csp(test_db, "csp-owner@example.com")
    intruder, _ = _seed_csp(test_db, "csp-intruder@example.com")
    report = CspStrReport(
        csp_id=owner_profile.id, trigger_type="unusual_transaction",
        trigger_detail="Single inbound transfer of S$48,000 from an offshore account.",
        decision=StrDecision.NOT_FILED, decision_by="John Lim",
        decision_date=NOW, decision_rationale=_RATIONALE,
    )
    test_db.add(report)
    test_db.commit()
    test_db.refresh(report)

    res = client.get(f"/api/v1/csp/str/{report.id}/record",
                     headers=_auth_header(intruder.email))
    assert res.status_code == 404
