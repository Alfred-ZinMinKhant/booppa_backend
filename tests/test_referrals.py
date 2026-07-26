"""The referral endpoints were both 500ing on every call.

`create` passed referred_email=None into a column the v10 migration created
NOT NULL (model said nullable) → IntegrityError. `redeem` compared the naive
`expires_at` read back from a `timestamp without time zone` column against an
aware `datetime.now(timezone.utc)` → TypeError. Neither had a test.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.core.models import Referral, User


@pytest.fixture
def referrer(test_db):
    user = User(
        id=uuid.uuid4(),
        email=f"referrer-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
    )
    test_db.add(user)
    test_db.commit()
    return user


def test_create_referral_without_an_email(client, referrer):
    resp = client.post("/api/referrals/create", json={"referrer_id": str(referrer.id)})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "PENDING"
    assert body["referral_code"]


def _referral(db, referrer_id, *, expires_in_days: int) -> Referral:
    row = Referral(
        id=uuid.uuid4(),
        referrer_id=referrer_id,
        referral_code=f"QA{uuid.uuid4().hex[:8].upper()}",
        status="PENDING",
        expires_at=datetime.utcnow() + timedelta(days=expires_in_days),
    )
    db.add(row)
    db.commit()
    return row


def test_redeem_live_referral(client, test_db, referrer):
    row = _referral(test_db, referrer.id, expires_in_days=90)

    resp = client.post(
        f"/api/referrals/redeem/{row.referral_code}",
        params={"user_id": str(uuid.uuid4())},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "SIGNED_UP"


def test_redeem_expired_referral_is_rejected(client, test_db, referrer):
    row = _referral(test_db, referrer.id, expires_in_days=-1)

    resp = client.post(
        f"/api/referrals/redeem/{row.referral_code}",
        params={"user_id": str(uuid.uuid4())},
    )

    assert resp.status_code == 400, resp.text
    assert "expired" in resp.json()["detail"].lower()
    test_db.refresh(row)
    assert row.status == "EXPIRED"
