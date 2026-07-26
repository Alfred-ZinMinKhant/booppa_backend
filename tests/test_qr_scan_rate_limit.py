"""The free QR scan is limited to once per month per email.

The lookup used `cast(Report.assessment_data["contact_email"], String)`, but
`assessment_data` is a plain `json` column, so that renders
`CAST(data -> 'contact_email' AS VARCHAR)` and yields `'"a@b.com"'` — quoted, so
it never equalled a bare email. The 429 branch was unreachable and the endpoint
was effectively unmetered.
"""

import uuid

from app.core.models import Report


def _free_scan_report(db, email: str) -> Report:
    report = Report(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        framework="pdpa_free_scan",
        company_name="Booppa QA",
        company_website="https://booppa.io",
        assessment_data={"contact_email": email, "tier": "free"},
        status="completed",
    )
    db.add(report)
    db.commit()
    return report


def test_second_free_scan_within_a_month_is_rejected(client, test_db):
    email = f"qr-{uuid.uuid4().hex[:8]}@example.com"
    _free_scan_report(test_db, email)

    resp = client.post(
        "/api/qr-scan", json={"website_url": "https://booppa.io", "email": email}
    )

    assert resp.status_code == 429, resp.text
    assert "once per month" in resp.json()["detail"]


def test_lookup_matches_only_the_same_email(test_db):
    """Same query the endpoint runs — asserted directly so this stays offline."""
    mine = f"qr-{uuid.uuid4().hex[:8]}@example.com"
    other = f"qr-{uuid.uuid4().hex[:8]}@example.com"
    _free_scan_report(test_db, mine)

    def _lookup(email: str):
        return (
            test_db.query(Report)
            .filter(
                Report.framework == "pdpa_free_scan",
                Report.assessment_data["contact_email"].as_string() == email,
            )
            .first()
        )

    assert _lookup(mine) is not None
    assert _lookup(other) is None
