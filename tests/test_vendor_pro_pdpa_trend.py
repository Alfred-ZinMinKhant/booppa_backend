"""Vendor Pro PDPA compliance trend endpoint."""
from datetime import datetime, timedelta, timezone

from tests._test_helpers import make_user, auth_headers


def _pdpa_report(db, user, score, days_ago):
    from app.core.models import Report
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    db.add(Report(
        owner_id=user.id,
        framework="pdpa_quick_scan",
        company_name="Acme",
        assessment_data={"compliance_score": score},
        status="completed",
        completed_at=when,
    ))
    db.commit()


def test_trend_returns_points_oldest_to_newest(client, test_db):
    user = make_user(test_db, email="pro-trend@booppa.io", plan="vendor_pro", company="Acme")
    _pdpa_report(test_db, user, 48, days_ago=120)
    _pdpa_report(test_db, user, 53, days_ago=60)
    _pdpa_report(test_db, user, 61, days_ago=1)

    r = client.get("/api/v1/vendor-pro/pdpa-trend", headers=auth_headers(user))
    assert r.status_code == 200, r.text
    pts = r.json()["points"]
    assert [p["score"] for p in pts] == [48, 53, 61]  # oldest → newest
    assert all("label" in p for p in pts)


def test_trend_empty_when_no_scans(client, test_db):
    user = make_user(test_db, email="pro-trend2@booppa.io", plan="vendor_pro", company="Acme")
    r = client.get("/api/v1/vendor-pro/pdpa-trend", headers=auth_headers(user))
    assert r.status_code == 200
    assert r.json() == {"points": []}


def test_trend_requires_vendor_pro(client, test_db):
    user = make_user(test_db, email="pro-trend3@booppa.io", plan="free", company="Acme")
    r = client.get("/api/v1/vendor-pro/pdpa-trend", headers=auth_headers(user))
    assert r.status_code == 403
