"""The TRM evidence demo must be reproducible from the admin panel, not just a shell.

`scripts/demo_trm_baseline.py` and `POST /admin/trm/demo-baseline` share one code
path (`app.services.trm_demo_harness.seed_and_generate`). These pin the seeding
contract — documented vs tested grading, no duplicate evidence on re-run — and
that the endpoint exists and is auth-gated.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.services.trm_demo_harness import DEMO_DOMAINS, DR_TEST_DATE, seed_and_generate


def _seed(db, email, mas_licence_type=None):
    # The worker path is exercised by the script end-to-end; here we only pin the
    # seeding contract, so the generation step is stubbed out.
    with patch("app.workers.tasks.run_suite_trm_baseline_for_user"):
        return seed_and_generate(
            customer_email=email, live_ai=False, capture_pdf=True, db=db,
            mas_licence_type=mas_licence_type,
        )


def test_seeds_documented_and_tested_evidence(test_db):
    from app.core.models import Organisation, TrmControl, TrmEvidence

    email = f"demo-{uuid.uuid4().hex[:8]}@booppa.io"
    result = _seed(test_db, email)

    assert result["user_email"] == email
    assert result["domains_analysed"] == DEMO_DOMAINS
    assert result["evidence_summary"]["Business Continuity and Disaster Recovery"] == (
        f"Tested — {DR_TEST_DATE:%d %b %Y}"
    )

    org = test_db.query(Organisation).filter(Organisation.id == result["org_id"]).one()
    controls = test_db.query(TrmControl).filter(
        TrmControl.organisation_id == org.id).all()
    # All 13 MAS TRM domains initialised, not just the three demonstrated.
    assert len(controls) == 13

    by_domain = {c.domain: c for c in controls}
    ev = {}
    for domain in DEMO_DOMAINS:
        ev[domain] = test_db.query(TrmEvidence).filter(
            TrmEvidence.control_id == by_domain[domain].id).all()

    assert len(ev["Cyber Security"]) == 2
    assert all(e.evidence_type == "documented" for e in ev["Cyber Security"])

    # The honest residual gap: a runbook on file, no drill. It must NOT be tested.
    assert len(ev["Incident Management"]) == 1
    assert ev["Incident Management"][0].evidence_type == "documented"
    assert ev["Incident Management"][0].tested_at is None

    dr = ev["Business Continuity and Disaster Recovery"][0]
    assert dr.evidence_type == "tested"
    assert dr.tested_at == DR_TEST_DATE
    assert dr.attestation and "3h58m" in dr.attestation

    # No licence class was seeded, so no FSM Notice is known to bind this
    # entity. The narrative must degrade to the Guidelines rather than name a
    # Notice — naming one here is the FSM-N06-for-everyone bug.
    for domain in ("Cyber Security", "Incident Management"):
        assert "FSM-N" not in by_domain[domain].gap_analysis
        assert "Guidelines" in by_domain[domain].gap_analysis


def test_seeded_gap_narratives_cite_the_notice_for_the_licence_class(test_db):
    """Gap narratives name the binding Notice by number — the right one.

    A bank gets FSM-N05/N06; an MPI gets FSM-N14 on the cyber-hygiene half and
    no Notice at all on the technology-risk half, because FSM-N13's scope
    (designated payment systems / DPT licensees) does not cover every MPI.
    """
    from app.core.models import TrmControl

    def gaps(licence):
        res = _seed(test_db, f"demo-{uuid.uuid4().hex[:8]}@booppa.io", licence)
        rows = test_db.query(TrmControl).filter(
            TrmControl.organisation_id == res["org_id"]).all()
        return {c.domain: c.gap_analysis for c in rows}

    bank = gaps("bank")
    assert "FSM-N06" in bank["Cyber Security"]
    assert "FSM-N05" in bank["Incident Management"]

    mpi = gaps("mpi")
    assert "FSM-N14" in mpi["Cyber Security"]
    assert "FSM-N06" not in mpi["Cyber Security"]
    assert "FSM-N" not in mpi["Incident Management"]
    assert "Guidelines" in mpi["Incident Management"]


def test_rerun_does_not_duplicate_evidence_or_controls(test_db):
    from app.core.models import TrmControl, TrmEvidence

    email = f"demo-{uuid.uuid4().hex[:8]}@booppa.io"
    first = _seed(test_db, email)
    second = _seed(test_db, email)

    # Same tenant and org reused — a QA re-run must not pile up accounts, and the
    # worker only ever reads the *earliest* org, so a second one would be invisible.
    assert second["user_id"] == first["user_id"]
    assert second["org_id"] == first["org_id"]

    controls = test_db.query(TrmControl).filter(
        TrmControl.organisation_id == first["org_id"]).all()
    assert len(controls) == 13

    demo_ids = [c.id for c in controls if c.domain in DEMO_DOMAINS]
    total = test_db.query(TrmEvidence).filter(
        TrmEvidence.control_id.in_(demo_ids)).count()
    assert total == 4  # 2 documented + 1 documented + 1 tested


def test_admin_demo_endpoint_is_registered_and_auth_gated(client):
    resp = client.post("/api/v1/admin/trm/demo-baseline",
                       json={"customer_email": "qa@booppa.io"})
    # Registered (not 404) and refuses an unauthenticated caller.
    assert resp.status_code == 401


# ── ACRA identity: a supplied UEN is a query, never a fact ───────────────────
#
# The harness used to hand the typed UEN straight to `record_verified_identity`,
# which persists without asking any registry. A demo cover then printed an
# unverified number under the heading "UEN", indistinguishable from a real
# registration. These pin the replacement: resolution goes through ACRA, and a
# miss leaves the account unverified rather than inventing an identity.

def _seed_with_uen(db, email, uen, *, acra_result):
    with patch("app.workers.tasks.run_suite_trm_baseline_for_user"), \
         patch("app.services.evidence_enricher.fetch_acra_status",
               new=AsyncMock(return_value=acra_result)):
        return seed_and_generate(
            customer_email=email, company_name="Meridian Demo Pte Ltd",
            uen=uen, live_ai=False, capture_pdf=True, db=db,
        )


def test_unmatched_uen_is_not_persisted_as_a_verified_identity(test_db):
    from app.core.models import User

    email = f"demo-{uuid.uuid4().hex[:8]}@booppa.io"
    result = _seed_with_uen(test_db, email, "201700000Z",
                            acra_result={"found": False})

    user = test_db.query(User).filter(User.email == email).one()
    assert user.uen is None, "an unverified UEN must never reach the account"
    assert result["acra"]["attempted"] is True
    assert result["acra"]["verified"] is False
    # The readout has to say so explicitly — a silent False is how a demo gets
    # screenshotted as if it were verified.
    assert "No ACRA match" in result["acra"]["note"]


def test_registry_confirmed_uen_is_persisted(test_db):
    from app.core.models import User

    email = f"demo-{uuid.uuid4().hex[:8]}@booppa.io"
    result = _seed_with_uen(test_db, email, "201700000Z", acra_result={
        "found": True, "live": True, "uen": "201700000Z",
        "registered_name": "MERIDIAN DEMO PTE. LTD.",
        "entity_status": "LIVE", "entity_type": "PTE LTD",
        "registration_date": "2017-01-01", "warning": None,
    })

    user = test_db.query(User).filter(User.email == email).one()
    assert user.uen == "201700000Z"
    assert user.legal_name == "MERIDIAN DEMO PTE. LTD."
    assert result["acra"]["verified"] is True
    assert result["acra"]["note"] is None


def test_no_uen_supplied_makes_no_second_acra_attempt(test_db):
    email = f"demo-{uuid.uuid4().hex[:8]}@booppa.io"
    result = _seed(test_db, email)
    assert result["acra"]["attempted"] is False
