"""Regressions for two ways the Compliance Evidence Pack cover sheet silently
never reached a paying buyer. Both were deterministic, both survived the hourly
`sweep_pending_cover_sheets` backstop (which re-runs the identical check and so
reproduces the identical failure), and neither raised or alerted.

1. `compliance_evidence_monthly` cycle starvation — "already delivered" was
   computed over the buyer's LIFETIME cover-sheet rows. Correct for the one-time
   S$799 SKU; fatal for the S$499/mo subscription, whose whole proposition is a
   fresh sheet for the SAME subject every month. From cycle 2 the subject was
   always already in `delivered`, every subject was skipped, `still_waiting`
   stayed False, and `pending_cover_sheet` was cleared with nothing sent —
   dropping the buyer out of the sweep's query for the life of the subscription.

2. Buyer address == support/DPO sentinel — the fallback-email guard blanked any
   address matching SUPPORT_EMAIL/COMPANY_DPO_EMAIL, re-resolved it by user_id,
   then re-applied the same sentinel test to the value it had just re-derived.
   For an account whose own address IS that address the recovery could never
   succeed, so the function returned before reaching any readiness check.

Pattern mirrors tests/test_cover_sheet_waits_for_evidence_pack.py.
"""
import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import sessionmaker

import app.services.fulfillment.helpers as wh
import app.workers.tasks as tasks_mod
from app.core.company import COMPANY_DPO_EMAIL
from app.core.models import User, Report, EvidencePack


def _install(test_db, monkeypatch):
    monkeypatch.setattr(wh, "SessionLocal", sessionmaker(bind=test_db.get_bind()))
    fired = []
    monkeypatch.setattr(
        tasks_mod.fulfill_cover_sheet_task,
        "apply_async",
        lambda *a, **k: fired.append(k) or None,
    )
    return fired


def _seed(test_db, email=None, *, domain="acme.test"):
    """Buyer with every cover-sheet input ready for `domain`."""
    user = User(
        email=email or f"cep-{uuid.uuid4().hex[:8]}@test.io",
        hashed_password="x",
        company="Acme Pte Ltd",
        pending_cover_sheet=True,
        compliance_evidence_rfp_ready=True,
    )
    test_db.add(user)
    test_db.commit()
    test_db.refresh(user)

    test_db.add(
        Report(
            id=uuid.uuid4(),
            owner_id=user.id,
            framework="pdpa_quick_scan",
            company_name="Acme Pte Ltd",
            company_website=f"https://{domain}",
            status="completed",
            assessment_data={"overall_risk_score": 46},
            created_at=datetime.utcnow(),
        )
    )
    test_db.commit()
    return user


def _add_pack(test_db, user, *, domain="acme.test", created_at=None, status="ready"):
    pack = EvidencePack(
        id=uuid.uuid4(),
        pack_id=f"BCEP-{uuid.uuid4().hex[:8]}",
        user_id=user.id,
        status=status,
        intake={"domain": domain},
        created_at=created_at or datetime.utcnow(),
    )
    test_db.add(pack)
    test_db.commit()
    return pack


def _add_delivered_sheet(test_db, user, *, domain="acme.test", created_at=None):
    """A cover sheet already issued for `domain` (i.e. a previous cycle)."""
    row = Report(
        id=uuid.uuid4(),
        owner_id=user.id,
        framework="compliance_evidence_pack",
        company_name="Acme Pte Ltd",
        company_website=domain,
        status="completed",
        assessment_data={"bundle_type": "compliance_evidence_pack"},
        created_at=created_at or datetime.utcnow(),
    )
    test_db.add(row)
    test_db.commit()
    return row


# ── 1. Monthly cycle starvation ──────────────────────────────────────────────

def test_new_monthly_cycle_fires_again_for_the_same_subject(test_db, monkeypatch):
    """A subscription cycle whose pack postdates last month's sheet must fire."""
    fired = _install(test_db, monkeypatch)
    user = _seed(test_db)
    # Last month's sheet, then THIS month's cycle marker.
    _add_delivered_sheet(test_db, user, created_at=datetime.utcnow() - timedelta(days=30))
    _add_pack(test_db, user, created_at=datetime.utcnow())

    wh._maybe_fire_cover_sheet(user.email)

    assert len(fired) == 1, "new cycle must re-issue the sheet for the same subject"
    assert fired[0]["kwargs"]["target_domain"] == "acme.test"


def test_one_time_buyer_is_not_re_sent_a_sheet(test_db, monkeypatch):
    """The lifetime behaviour must be preserved for the one-time S$799 SKU: a
    sheet already issued AFTER this cycle's pack is this cycle's sheet."""
    fired = _install(test_db, monkeypatch)
    user = _seed(test_db)
    pack_at = datetime.utcnow() - timedelta(hours=2)
    _add_pack(test_db, user, created_at=pack_at)
    _add_delivered_sheet(test_db, user, created_at=pack_at + timedelta(minutes=5))

    wh._maybe_fire_cover_sheet(user.email)

    assert fired == [], "must not re-send a sheet already issued for this cycle"
    test_db.expire_all()
    assert test_db.query(User).filter(User.id == user.id).first().pending_cover_sheet is False


# ── 2. Buyer address collides with the support/DPO sentinel ──────────────────

def test_buyer_whose_email_is_the_support_sentinel_still_gets_a_sheet(test_db, monkeypatch):
    """The fallback guard must not reject an address it re-derived from a real
    User row — a User resolved by primary key IS a buyer."""
    fired = _install(test_db, monkeypatch)
    user = _seed(test_db, email=COMPANY_DPO_EMAIL)
    _add_pack(test_db, user)

    # Inline trigger shape: fulfillment passes both the address and the user id.
    wh._maybe_fire_cover_sheet(user.email, user_id=str(user.id))

    assert len(fired) == 1, "sentinel-valued buyer address must not block delivery"


def test_sweep_delivers_to_sentinel_addressed_buyer(test_db, monkeypatch):
    """The sweep must be at least as capable as the inline trigger it backstops:
    passing only the email made it return before any readiness check."""
    fired = _install(test_db, monkeypatch)
    user = _seed(test_db, email=COMPANY_DPO_EMAIL)
    _add_pack(test_db, user)

    wh._maybe_fire_cover_sheet(user.email, user_id=str(user.id))

    assert len(fired) == 1


# ── 3. The flag must never be surrendered without delivering ─────────────────

def test_pending_flag_survives_a_pass_that_delivers_nothing(test_db, monkeypatch):
    """`pending_cover_sheet` going False removes the buyer from the sweep, so a
    wrong clear is terminal. A pass that neither queues a sheet nor accounts for
    every subject as served this cycle must keep the flag."""
    fired = _install(test_db, monkeypatch)
    user = _seed(test_db)
    # Pack for a subject the PDPA scan does not cover -> deferral, not delivery.
    _add_pack(test_db, user, domain="other-subject.test")

    wh._maybe_fire_cover_sheet(user.email)

    assert fired == []
    test_db.expire_all()
    assert (
        test_db.query(User).filter(User.id == user.id).first().pending_cover_sheet is True
    ), "flag must be retained so the sweep keeps re-checking"
