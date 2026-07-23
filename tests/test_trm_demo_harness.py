"""The TRM evidence demo must be reproducible from the admin panel, not just a shell.

`scripts/demo_trm_baseline.py` and `POST /admin/trm/demo-baseline` share one code
path (`app.services.trm_demo_harness.seed_and_generate`). These pin the seeding
contract — documented vs tested grading, no duplicate evidence on re-run — and
that the endpoint exists and is auth-gated.
"""
import uuid
from unittest.mock import patch

import pytest

from app.services.trm_demo_harness import DEMO_DOMAINS, DR_TEST_DATE, seed_and_generate


def _seed(db, email):
    # The worker path is exercised by the script end-to-end; here we only pin the
    # seeding contract, so the generation step is stubbed out.
    with patch("app.workers.tasks.run_suite_trm_baseline_for_user"):
        return seed_and_generate(
            customer_email=email, live_ai=False, capture_pdf=True, db=db
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

    # Gap narratives cite the binding notice by number, not a generic "MAS says".
    assert "655/FSM-N06" in by_domain["Cyber Security"].gap_analysis
    assert "644/FSM-N05" in by_domain["Incident Management"].gap_analysis


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
