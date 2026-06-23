"""Pro Suite multi-entity TRM comparison (Sprint 9e)."""
import uuid

from tests._test_helpers import make_user, make_org, auth_headers


def _suite_user(db, email, plan="pro_suite", company="Acme Group", parent=None):
    u = make_user(db, email=email, plan=plan, company=company)
    u.stripe_subscription_id = f"sub_{uuid.uuid4().hex[:12]}"
    if parent is not None:
        u.parent_user_id = parent.id
    db.commit(); db.refresh(u)
    return u


def _control(db, org, domain, status, risk=None):
    from app.core.models_enterprise import TrmControl
    c = TrmControl(organisation_id=org.id, domain=domain,
                   control_ref="TRM-X", status=status, risk_rating=risk)
    db.add(c); db.commit()
    return c


def test_comparison_rolls_up_parent_and_subsidiaries(client, test_db):
    parent = _suite_user(test_db, "grp-parent@booppa.io")
    p_org = make_org(test_db, owner=parent, tier="pro")
    # Parent: 2 compliant of 2 controls → 100%.
    _control(test_db, p_org, "Cyber Security", "compliant")
    _control(test_db, p_org, "Data and Information Management", "compliant")

    child = _suite_user(test_db, "grp-child@booppa.io", company="Sub B", parent=parent)
    c_org = make_org(test_db, owner=child, tier="pro")
    # Subsidiary: 0 compliant, 1 open critical → lags far behind the parent.
    _control(test_db, c_org, "Cyber Security", "gap", risk="critical")

    resp = client.get("/api/vendor/trm/subsidiary-comparison", headers=auth_headers(parent))
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["entity_count"] == 2
    by_name = {e["name"]: e for e in body["entities"]}
    assert by_name["Acme Group"]["is_parent"] is True
    assert by_name["Acme Group"]["compliant_pct"] == 100
    assert by_name["Sub B"]["compliant_pct"] == 0
    assert by_name["Sub B"]["critical_open"] == 1
    # Per-domain matrix is present for side-by-side rendering.
    assert by_name["Sub B"]["domain_status"]["Cyber Security"] == "gap"
    # Lag + critical alerts fired for the trailing subsidiary.
    assert any("significantly behind" in a for a in body["alerts"])
    assert any("critical" in a for a in body["alerts"])


def test_subsidiary_cannot_view_comparison(client, test_db):
    parent = _suite_user(test_db, "grp-parent2@booppa.io")
    child = _suite_user(test_db, "grp-child2@booppa.io", company="Sub", parent=parent)
    resp = client.get("/api/vendor/trm/subsidiary-comparison", headers=auth_headers(child))
    assert resp.status_code == 400
