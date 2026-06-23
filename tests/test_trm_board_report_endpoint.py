"""Self-serve MAS TRM board report endpoints (Sprint 9d full-stack)."""
import uuid
from datetime import datetime, timezone

from tests._test_helpers import make_user, make_org, auth_headers


def _suite_user(db, email, plan="pro_suite"):
    u = make_user(db, email=email, plan=plan, company="Acme Suite")
    u.stripe_subscription_id = f"sub_{uuid.uuid4().hex[:12]}"
    db.commit(); db.refresh(u)
    return u


def test_latest_returns_unavailable_when_none(client, test_db):
    user = _suite_user(test_db, "board-none@booppa.io")
    r = client.get("/api/vendor/trm/board-report/latest", headers=auth_headers(user))
    assert r.status_code == 200
    assert r.json() == {"available": False}


def test_latest_returns_report_with_fresh_url(client, test_db):
    from app.core.models import Report

    user = _suite_user(test_db, "board-have@booppa.io")
    make_org(test_db, owner=user, tier="pro")
    test_db.add(Report(
        owner_id=user.id,
        framework="trm_board_report",
        company_name="Acme Suite",
        assessment_data={"compliant_pct": 62, "s3_key": f"reports/trm-board-{user.id}-202606.pdf",
                         "plan_label": "Pro Suite"},
        status="completed",
        file_key=f"reports/trm-board-{user.id}-202606.pdf",
        s3_url="https://stale.example/old",
        completed_at=datetime.now(timezone.utc),
    ))
    test_db.commit()

    r = client.get("/api/vendor/trm/board-report/latest", headers=auth_headers(user))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True
    assert body["compliant_pct"] == 62
    assert body["plan_label"] == "Pro Suite"
    assert body["download_url"]  # re-presigned (or fallback) URL present


def test_generate_enqueues_task(client, test_db, mocker):
    user = _suite_user(test_db, "board-gen@booppa.io")
    fake = mocker.patch("app.workers.tasks.run_trm_board_report_for_user.delay")
    r = client.post("/api/vendor/trm/board-report/generate", headers=auth_headers(user))
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "queued"
    fake.assert_called_once_with(str(user.id))
