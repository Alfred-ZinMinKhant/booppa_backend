"""The Compliance Evidence Pack cover sheet is the centerpiece: it must index
every deliverable, so `_maybe_fire_cover_sheet` must NOT fire until the PDPA
Snapshot, the RFP Complete kit, AND the BCEP 7-document pack are all ready.

A 7-day grace backstop fires the sheet with PDPA + RFP only if the buyer never
completes the evidence-pack intake, so nobody is left without a cover sheet.

Pattern mirrors tests/test_fulfillment_email_alerts.py: pin the webhook module's
SessionLocal to the test engine and spy on the queued task.
"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import sessionmaker

import app.api.stripe_webhook as wh
import app.workers.tasks as tasks_mod
from app.core.models import User, Report
from app.core.models_v13 import EvidencePack


def _seed_ready_pdpa_and_rfp(test_db, *, pdpa_age_days: int = 0) -> User:
    """User with pending_cover_sheet + RFP ready + a completed PDPA report."""
    email = f"cep-{uuid.uuid4().hex[:8]}@test.io"
    user = User(
        email=email,
        hashed_password="x",
        company="Acme Pte Ltd",
        pending_cover_sheet=True,
        compliance_evidence_rfp_ready=True,
    )
    test_db.add(user)
    test_db.commit()
    test_db.refresh(user)

    created = datetime.utcnow() - timedelta(days=pdpa_age_days)
    test_db.add(
        Report(
            id=uuid.uuid4(),
            owner_id=user.id,
            framework="pdpa_quick_scan",
            status="completed",
            created_at=created,
        )
    )
    test_db.commit()
    return user


def _install(test_db, monkeypatch):
    """Pin SessionLocal to the test engine and spy on the cover-sheet task."""
    monkeypatch.setattr(wh, "SessionLocal", sessionmaker(bind=test_db.get_bind()))
    fired = []
    monkeypatch.setattr(
        tasks_mod.fulfill_cover_sheet_task,
        "apply_async",
        lambda *a, **k: fired.append(k) or None,
    )
    return fired


def test_does_not_fire_while_bcep_pack_pending(test_db, monkeypatch):
    fired = _install(test_db, monkeypatch)
    user = _seed_ready_pdpa_and_rfp(test_db)
    # No ready EvidencePack — only an intake_pending one.
    test_db.add(
        EvidencePack(
            id=uuid.uuid4(),
            pack_id=f"BCEP-{uuid.uuid4().hex[:8]}",
            user_id=user.id,
            status="intake_pending",
        )
    )
    test_db.commit()

    wh._maybe_fire_cover_sheet(user.email)

    assert fired == []  # PDPA + RFP ready, but the 7-doc pack is not — must wait.
    test_db.expire_all()
    assert test_db.query(User).get(user.id).pending_cover_sheet is True


def test_fires_once_bcep_pack_ready(test_db, monkeypatch):
    fired = _install(test_db, monkeypatch)
    user = _seed_ready_pdpa_and_rfp(test_db)
    test_db.add(
        EvidencePack(
            id=uuid.uuid4(),
            pack_id=f"BCEP-{uuid.uuid4().hex[:8]}",
            user_id=user.id,
            status="ready",
        )
    )
    test_db.commit()

    wh._maybe_fire_cover_sheet(user.email)

    assert len(fired) == 1  # all three inputs ready — fires.
    test_db.expire_all()
    assert test_db.query(User).get(user.id).pending_cover_sheet is False


def test_backstop_fires_after_grace_without_bcep(test_db, monkeypatch):
    fired = _install(test_db, monkeypatch)
    # PDPA completed > grace window ago, still no ready pack (buyer abandoned intake).
    user = _seed_ready_pdpa_and_rfp(
        test_db, pdpa_age_days=wh._COVER_SHEET_BCEP_GRACE_DAYS + 1
    )

    wh._maybe_fire_cover_sheet(user.email)

    assert len(fired) == 1  # grace elapsed — fire with PDPA + RFP only.
    test_db.expire_all()
    assert test_db.query(User).get(user.id).pending_cover_sheet is False
