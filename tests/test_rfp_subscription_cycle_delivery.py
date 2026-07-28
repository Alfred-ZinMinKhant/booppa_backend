"""`compliance_evidence_monthly` must deliver every promised document on the day
of each cycle — not only the first — matching the one-time S$799 pack.

The RFP kit was the one component with no subscription-cycle path. The BCEP pack
reuses the buyer's last intake on renewal; the RFP instead minted a fresh
PendingRfpIntake every month (a *second* one, since the monthly nudge already
created a pre-filled row ~6 days earlier). The subscriber therefore had to re-fill
a brief they had already completed, and the cover sheet — whose `rfp_done` check
matches ANY completed kit for the subject — fired anyway, anchoring over the
PRIOR month's kit.

Policy implemented here (option B):
  * Buyer confirmed this cycle via the nudge -> their fresh kit stands, untouched.
  * Buyer did not -> regenerate from last cycle's brief so the document still
    arrives on renewal day, flagged as facts not re-confirmed.
  * No prior brief at all (first cycle) -> request a brief, as before.
"""
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

import app.services.fulfillment.bundles as bundles
from app.core.models import User, Report, PendingRfpIntake

WEBSITE = "https://acme.test"


@pytest.fixture
def queued(monkeypatch, test_db):
    """Capture fulfill_rfp_task dispatches and pin SessionLocal to the test DB."""
    monkeypatch.setattr(bundles, "SessionLocal", sessionmaker(bind=test_db.get_bind()))
    calls = []
    import app.workers.tasks as tasks_mod

    monkeypatch.setattr(
        tasks_mod.fulfill_rfp_task, "delay", lambda **k: calls.append(k) or None
    )
    return calls


def _user(test_db):
    u = User(
        email=f"ce-{uuid.uuid4().hex[:8]}@test.io",
        hashed_password="x",
        company="Acme Pte Ltd",
        website=WEBSITE,
    )
    test_db.add(u)
    test_db.commit()
    test_db.refresh(u)
    return u


def _prior_kit(test_db, user, *, age_days, desc="Procure a PDPA-compliant CRM."):
    """A completed rfp_complete kit from `age_days` ago, with its brief persisted."""
    r = Report(
        id=uuid.uuid4(),
        owner_id=user.id,
        framework="rfp_complete",
        company_name="Acme Pte Ltd",
        company_website=WEBSITE,
        status="completed",
        assessment_data={
            "product_type": "rfp_complete",
            "intake_rfp_description": desc,
            "intake_data": {"budget": "SGD 50k", "timeline": "Q3"},
        },
        created_at=datetime.utcnow() - timedelta(days=age_days),
    )
    test_db.add(r)
    test_db.commit()
    return r


# ── _prior_rfp_brief policy ──────────────────────────────────────────────────

def test_reuses_last_cycles_brief_when_buyer_did_not_reconfirm(test_db):
    user = _user(test_db)
    _prior_kit(test_db, user, age_days=30)

    result = bundles._prior_rfp_brief(test_db, user.id, WEBSITE)

    assert result is not None, "renewal must regenerate rather than stall on a brief"
    desc, intake = result
    assert desc == "Procure a PDPA-compliant CRM."
    assert intake["budget"] == "SGD 50k", "last cycle's answers must carry over"
    assert intake["_facts_confirmed_this_cycle"] is False, (
        "carried-over facts must never read as re-attested"
    )


def test_does_not_overwrite_a_kit_the_buyer_confirmed_this_cycle(test_db):
    """The 6-day nudge produced a fresh kit — regenerating would replace
    re-attested facts with older ones."""
    user = _user(test_db)
    _prior_kit(test_db, user, age_days=2)

    assert bundles._prior_rfp_brief(test_db, user.id, WEBSITE) is None


def test_first_cycle_has_no_brief_to_reuse(test_db):
    user = _user(test_db)

    assert bundles._prior_rfp_brief(test_db, user.id, WEBSITE) is None


def test_brief_is_scoped_to_the_subject(test_db):
    """An account subscribing for several companies must not carry one
    company's brief onto another's kit."""
    user = _user(test_db)
    other = _prior_kit(test_db, user, age_days=30)
    other.company_website = "https://other-co.test"
    test_db.commit()

    assert bundles._prior_rfp_brief(test_db, user.id, WEBSITE) is None


# ── No duplicate brief requests ──────────────────────────────────────────────

def test_outstanding_brief_request_is_reused_not_duplicated(test_db, queued):
    """The monthly nudge already created a pending row; the cycle must not add
    a second one and send the buyer two brief links."""
    import asyncio

    user = _user(test_db)
    existing = PendingRfpIntake(
        user_id=user.id,
        session_id="nudge-session",
        rfp_product_type="rfp_complete",
        bundle_source="compliance_evidence_monthly_refresh",
        status="pending",
    )
    test_db.add(existing)
    test_db.commit()

    asyncio.run(
        bundles._fulfill_bundle(
            product_type="compliance_evidence_pack",
            report_id=None,
            customer_email=user.email,
            metadata={"vendor_url": WEBSITE, "company_name": "Acme Pte Ltd"},
            session_id="cycle-session",
        )
    )

    rows = (
        test_db.query(PendingRfpIntake)
        .filter(PendingRfpIntake.user_id == user.id)
        .all()
    )
    assert len(rows) == 1, "must reuse the outstanding brief request, not stack another"


# ── Test checkout keeps same-day delivery ────────────────────────────────────

def test_test_checkout_still_fulfils_the_kit_immediately(test_db, queued):
    """The admin test checkout must keep delivering everything on the day it is
    run — it must never be diverted into the renewal or brief-request paths."""
    import asyncio

    user = _user(test_db)

    asyncio.run(
        bundles._fulfill_bundle(
            product_type="compliance_evidence_pack",
            report_id=None,
            customer_email=user.email,
            metadata={
                "vendor_url": WEBSITE,
                "company_name": "Acme Pte Ltd",
                "test_simulation": True,
                "rfp_description": "Canned QA brief.",
            },
            session_id="sim-session",
        )
    )

    assert len(queued) == 1, "test checkout must fulfil the RFP kit immediately"
    assert queued[0]["allow_incomplete"] is True
    assert (
        test_db.query(PendingRfpIntake).filter(PendingRfpIntake.user_id == user.id).count()
        == 0
    ), "test checkout must not ask the tester to fill a brief"
