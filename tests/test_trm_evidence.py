"""TRM evidence workspace: per-control upload / list / delete + evidence_count.

Covers the §7 additions to app/api/vendor_features.py. Uses moto S3 (s3_bucket).
"""
import uuid

from tests._test_helpers import make_user, make_org, auth_headers


def _suite_user(db, email):
    """A paid Standard/Pro-Suite vendor — the tier that unlocks the TRM dashboard."""
    user = make_user(db, email=email, plan="pro_suite", company="Acme Suite")
    user.stripe_subscription_id = f"sub_{uuid.uuid4().hex[:12]}"
    db.commit(); db.refresh(user)
    return user


def _control(db, org):
    from app.core.models import TrmControl
    ctrl = TrmControl(organisation_id=org.id, domain="Technology Risk Governance",
                      control_ref="TRM-1", status="not_started")
    db.add(ctrl); db.commit(); db.refresh(ctrl)
    return ctrl


def test_evidence_upload_list_delete_roundtrip(client, test_db, s3_bucket):
    user = _suite_user(test_db, "trm-ev@booppa.io")
    org = make_org(test_db, owner=user, tier="pro")
    ctrl = _control(test_db, org)
    headers = auth_headers(user)
    base = f"/api/vendor/trm/{ctrl.id}/evidence"

    # Upload
    up = client.post(
        base,
        files={"file": ("policy.pdf", b"%PDF-1.4 evidence", "application/pdf")},
        headers=headers,
    )
    assert up.status_code == 200, up.text
    ev_id = up.json()["id"]
    assert up.json()["file_name"] == "policy.pdf"
    assert len(up.json()["hash_value"]) == 64

    # List (with a presigned download URL)
    lst = client.get(base, headers=headers)
    assert lst.status_code == 200
    items = lst.json()["items"]
    assert len(items) == 1 and items[0]["id"] == ev_id
    assert items[0]["download_url"]

    # evidence_count surfaces on the TRM dashboard
    trm = client.get("/api/vendor/trm", headers=headers).json()
    row = next(i for i in trm["items"] if i["id"] == str(ctrl.id))
    assert row["evidence_count"] == 1

    # Delete
    dele = client.delete(f"{base}/{ev_id}", headers=headers)
    assert dele.status_code == 200 and dele.json()["deleted"] is True
    assert client.get(base, headers=headers).json()["items"] == []


def test_evidence_defaults_to_documented(client, test_db, s3_bucket):
    user = _suite_user(test_db, "trm-ev-doc@booppa.io")
    org = make_org(test_db, owner=user, tier="pro")
    ctrl = _control(test_db, org)
    base = f"/api/vendor/trm/{ctrl.id}/evidence"

    up = client.post(
        base,
        files={"file": ("policy.pdf", b"%PDF-1.4 doc", "application/pdf")},
        headers=auth_headers(user),
    )
    assert up.status_code == 200, up.text
    assert up.json()["evidence_type"] == "documented"
    assert up.json()["tested_at"] is None


def test_tested_evidence_roundtrip(client, test_db, s3_bucket):
    user = _suite_user(test_db, "trm-ev-tested@booppa.io")
    org = make_org(test_db, owner=user, tier="pro")
    ctrl = _control(test_db, org)
    base = f"/api/vendor/trm/{ctrl.id}/evidence"

    up = client.post(
        base,
        files={"file": ("dr_test.pdf", b"%PDF-1.4 dr test report", "application/pdf")},
        data={
            "evidence_type": "tested",
            "tested_at": "2026-03-15",
            "attestation": "Annual DR failover test — 3h58m recovery, verified by Head of IT.",
        },
        headers=auth_headers(user),
    )
    assert up.status_code == 200, up.text
    assert up.json()["evidence_type"] == "tested"
    assert up.json()["tested_at"].startswith("2026-03-15")

    item = client.get(base, headers=auth_headers(user)).json()["items"][0]
    assert item["evidence_type"] == "tested"
    assert item["tested_at"].startswith("2026-03-15")
    assert "DR failover" in item["attestation"]


def test_tested_at_must_be_iso(client, test_db, s3_bucket):
    user = _suite_user(test_db, "trm-ev-bad@booppa.io")
    org = make_org(test_db, owner=user, tier="pro")
    ctrl = _control(test_db, org)
    r = client.post(
        f"/api/vendor/trm/{ctrl.id}/evidence",
        files={"file": ("p.pdf", b"%PDF", "application/pdf")},
        data={"evidence_type": "tested", "tested_at": "March 2026"},
        headers=auth_headers(user),
    )
    assert r.status_code == 422


def test_rejects_unsupported_extension(client, test_db, s3_bucket):
    user = _suite_user(test_db, "trm-ev-ext@booppa.io")
    org = make_org(test_db, owner=user, tier="pro")
    ctrl = _control(test_db, org)
    r = client.post(
        f"/api/vendor/trm/{ctrl.id}/evidence",
        files={"file": ("malware.exe", b"MZ", "application/octet-stream")},
        headers=auth_headers(user),
    )
    assert r.status_code == 422


def test_requires_auth(client, test_db):
    # No control needs to exist — auth is checked first.
    r = client.get(f"/api/vendor/trm/{uuid.uuid4()}/evidence")
    assert r.status_code == 401


def test_cannot_touch_another_orgs_control(client, test_db, s3_bucket):
    owner = _suite_user(test_db, "trm-owner@booppa.io")
    org = make_org(test_db, owner=owner, tier="pro")
    ctrl = _control(test_db, org)

    intruder = _suite_user(test_db, "trm-intruder@booppa.io")
    make_org(test_db, owner=intruder, tier="pro")

    r = client.post(
        f"/api/vendor/trm/{ctrl.id}/evidence",
        files={"file": ("p.pdf", b"%PDF", "application/pdf")},
        headers=auth_headers(intruder),
    )
    assert r.status_code == 404
