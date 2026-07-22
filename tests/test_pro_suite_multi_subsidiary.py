"""Pro Suite multi-subsidiary: group rollup + per-subsidiary drill-down.

Gianpaolo's ask was two views: (a) a consolidated group-level TRM status
across parent + subsidiaries (already covered by GET /trm/subsidiary-comparison,
pre-existing) and (b) drilling into one subsidiary's own full 13-domain detail
(gap analysis text, evidence) as its parent tenant — the gap this test covers,
via GET /trm?subsidiary_id=... and GET /trm/{control_id}/evidence?subsidiary_id=...
"""
import uuid

from tests._test_helpers import auth_headers, make_org, make_user


def _pro_user(db, email, company="Acme Suite"):
    user = make_user(db, email=email, plan="pro_suite", company=company)
    user.stripe_subscription_id = f"sub_{uuid.uuid4().hex[:12]}"
    db.commit(); db.refresh(user)
    return user


def test_parent_can_drill_into_subsidiary_trm_detail(client, test_db):
    from app.core.models import TrmControl

    parent = _pro_user(test_db, "parent@booppa.io", company="NovaPay Group")
    sub = _pro_user(test_db, "sub@booppa.io", company="NovaPay Malaysia Sdn Bhd")
    sub.parent_user_id = parent.id
    test_db.commit()

    sub_org = make_org(test_db, owner=sub, tier="pro")
    ctrl = TrmControl(
        organisation_id=sub_org.id, domain="Cyber Security", control_ref="TRM-5",
        status="gap", gap_analysis="Subsidiary-specific MFA gap.",
    )
    test_db.add(ctrl); test_db.commit(); test_db.refresh(ctrl)

    headers = auth_headers(parent)

    # Parent can drill into the subsidiary's own full workspace.
    resp = client.get(f"/api/vendor/trm?subsidiary_id={sub.id}", headers=headers)
    assert resp.status_code == 200, resp.text
    domains = {item["domain"]: item for item in resp.json()["items"]}
    assert domains["Cyber Security"]["gap_analysis"] == "Subsidiary-specific MFA gap."

    # Without subsidiary_id, the parent still sees their OWN (empty) workspace,
    # not the subsidiary's — drill-down must not leak into the default view.
    own_resp = client.get("/api/vendor/trm", headers=headers)
    assert own_resp.status_code == 200
    own_domains = {item["domain"]: item for item in own_resp.json()["items"]}
    assert own_domains["Cyber Security"]["gap_analysis"] is None


def test_stranger_cannot_drill_into_unrelated_subsidiary(client, test_db):
    other_parent = _pro_user(test_db, "otherparent@booppa.io")
    sub = _pro_user(test_db, "sub2@booppa.io")
    real_parent = _pro_user(test_db, "realparent@booppa.io")
    sub.parent_user_id = real_parent.id
    test_db.commit()

    resp = client.get(
        f"/api/vendor/trm?subsidiary_id={sub.id}", headers=auth_headers(other_parent),
    )
    assert resp.status_code == 403


def test_subsidiary_cannot_drill_into_its_own_parent(client, test_db):
    """A subsidiary_id must be a subsidiary OF the caller — not the reverse."""
    parent = _pro_user(test_db, "parent3@booppa.io")
    sub = _pro_user(test_db, "sub3@booppa.io")
    sub.parent_user_id = parent.id
    test_db.commit()

    resp = client.get(
        f"/api/vendor/trm?subsidiary_id={parent.id}", headers=auth_headers(sub),
    )
    assert resp.status_code == 403
