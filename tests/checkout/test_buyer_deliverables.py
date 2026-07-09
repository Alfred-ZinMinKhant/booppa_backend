"""Behavioural coverage for the buyer proof-of-value deliverables.

The five buyer Celery tasks + demo fire-all shipped with zero tests. This file
locks in the guarantees that actually protect the buyer: the demo fire-all only
fires on a Stripe test-mode event (never for a live buyer), tiering attaches the
report PDF only for Pro/Enterprise, the snapshot/certificate task actually sends
(regression for a missing `branded_email_html` import), demo anchoring is
gas-free, and the tender-push dedup ledger is enforced at the schema level.
"""
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from tests.fixtures.stripe_events import wrap_event

BUYER_SUB = "buyer_pro_monthly"


def _seed_buyer(db, *, plan="buyer_pro", company="Acme Procurement"):
    from app.core.models import User
    u = User(
        email=f"buyer+{uuid.uuid4().hex[:8]}@booppa.io",
        hashed_password="x", role="BUYER", plan=plan, company=company,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# ── 1. Demo gate: only a Stripe test-mode event may arm the fire-all ─────────

@pytest.mark.parametrize(
    "livemode, expect_demo",
    [(False, True), (True, False), ("__missing__", False)],
    ids=["livemode-false", "livemode-true", "livemode-missing"],
)
def test_webhook_demo_flag_tracks_livemode_strictly(
    livemode, expect_demo, client, post_webhook, stripe_session_factory, mocker
):
    """`demo` passed to _activate_subscription must be True only when the event's
    livemode is *exactly* False — a true or absent value can never arm the demo
    fire-all for a real live buyer."""
    from unittest.mock import AsyncMock

    fake_activate = AsyncMock(return_value=None)
    mocker.patch("app.services.fulfillment.subscriptions._activate_subscription", fake_activate)

    session = stripe_session_factory(BUYER_SUB)
    event = wrap_event(session)
    if livemode == "__missing__":
        event.pop("livemode", None)
    else:
        event["livemode"] = livemode

    resp = post_webhook(event)
    assert resp.status_code == 200
    fake_activate.assert_awaited_once()
    assert fake_activate.await_args.kwargs.get("demo") is expect_demo


# ── 2. Fire-all fans out one demo copy of every deliverable ──────────────────

def test_demo_fireall_fans_out_all_arms(test_db, monkeypatch):
    from app.workers import tasks as t

    calls = {name: [] for name in (
        "buyer_tender_fit_push_task", "buyer_supplier_snapshot_task",
        "buyer_supplier_drift_alert_task", "buyer_procurement_digest_task",
    )}

    def _rec(name):
        def _delay(*a, **k):
            calls[name].append((a, k))
        return _delay

    for name in calls:
        monkeypatch.setattr(getattr(t, name), "delay", _rec(name))

    user = _seed_buyer(test_db)
    res = t.buyer_demo_fireall_task(str(user.id), user.email, product_type=BUYER_SUB)

    # 5 arms: tender push, snapshot, certificate, drift, digest.
    assert res["fired"] == 5
    assert len(calls["buyer_supplier_snapshot_task"]) == 2  # snapshot + certificate
    assert calls["buyer_supplier_snapshot_task"][0][1]["is_certificate"] is False
    assert calls["buyer_supplier_snapshot_task"][1][1]["is_certificate"] is True
    # Every arm runs in demo mode.
    for name in calls:
        for _a, k in calls[name]:
            assert k.get("demo") is True


# ── 3. Digest: Procurement Report PDF attached for every tier ────────────────

def test_digest_starter_attaches_report_pdf(test_db, email_capture):
    """Phase B: the full Procurement Intelligence Report now ships to every tier,
    Starter included — no buyer digest is attachment-less anymore."""
    from app.workers.tasks import buyer_procurement_digest_task
    user = _seed_buyer(test_db, plan="buyer_starter")
    buyer_procurement_digest_task(
        str(user.id), user.email, product_type="buyer_starter_monthly",
    )
    assert len(email_capture) == 1
    atts = email_capture[0]["attachments"]
    assert any(a[0].endswith(".pdf") and a[1].startswith(b"%PDF") for a in atts)


def test_digest_pro_attaches_report_pdf(test_db, email_capture):
    from app.workers.tasks import buyer_procurement_digest_task
    user = _seed_buyer(test_db, plan="buyer_pro")
    buyer_procurement_digest_task(
        str(user.id), user.email, product_type=BUYER_SUB,
    )
    assert len(email_capture) == 1
    atts = email_capture[0]["attachments"]
    assert atts and atts[0][0].endswith(".pdf")
    assert atts[0][1].startswith(b"%PDF")


# ── 4. Snapshot/certificate task actually sends (branded_email_html regression)

def test_snapshot_task_sends_in_demo_mode(test_db, email_capture):
    """Regression: buyer_supplier_snapshot_task referenced branded_email_html
    without importing it, so every snapshot/certificate raised NameError and
    failed silently. Demo mode also proves anchoring is gas-free (mock hash)."""
    from app.workers.tasks import buyer_supplier_snapshot_task
    user = _seed_buyer(test_db)
    buyer_supplier_snapshot_task(
        str(user.id), user.email, "sample-supplier", "Sample Supplier Pte Ltd",
        None, BUYER_SUB, is_certificate=True, demo=True,
    )
    assert len(email_capture) == 1
    msg = email_capture[0]
    assert msg["subject"].startswith("[DEMO] ")
    assert msg["attachments"] and msg["attachments"][0][1].startswith(b"%PDF")


# ── 5. Tender-push dedup ledger enforced at the schema level ──────────────────

def test_buyer_tender_push_ledger_is_unique_per_buyer_tender(test_db):
    from app.core.models import BuyerTenderPush
    user = _seed_buyer(test_db)
    test_db.add(BuyerTenderPush(buyer_user_id=user.id, tender_no="T-0001", sector="IT"))
    test_db.commit()
    test_db.add(BuyerTenderPush(buyer_user_id=user.id, tender_no="T-0001", sector="IT"))
    with pytest.raises(IntegrityError):
        test_db.commit()
    test_db.rollback()


def test_tender_push_sweep_noops_on_empty_window(test_db):
    from app.workers.tasks import buyer_tender_fit_push_sweep_task
    res = buyer_tender_fit_push_sweep_task(lookback_minutes=1)
    assert res.get("pushes", 0) == 0
