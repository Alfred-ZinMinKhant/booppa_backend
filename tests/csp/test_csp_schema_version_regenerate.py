"""Staleness + free-regenerate gates for CSP deliverables.

Context: tipping-off was cited as CDSA s.48A across the CSP pack, then
"corrected" to s.48; the offence is actually s.57. Already-issued
PDFs sit at fixed S3 keys with their hashes anchored on-chain, so the correction
is surfaced as an "outdated — regenerate free" path rather than a forced reissue.

What these pin:
  1. A row with no schema stamp reads as OUTDATED, not current. This is the whole
     mechanism — every pack delivered before the constant existed carries NULL and
     is exactly the population that needs correcting. A `or CURRENT` slip anywhere
     in the ladder would silently mark those packs clean and strand them.
  2. A current row is NOT offered a regeneration, and the endpoint 409s. Each run
     is 8 DeepSeek completions billed to us; an ungated endpoint is unbounded cost
     a customer can trigger in a loop.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.core.models import (
    CspAmlProgramme,
    CspOrganisation,
    CspOrgMembership,
    CspProfile,
    Report,
)
from app.services.csp_baseline_generator import CSP_BASELINE_SCHEMA_VERSION
from app.services.csp_doc_generator import CSP_DOC_SCHEMA_VERSION
from tests._test_helpers import auth_headers, make_user


@pytest.fixture
def csp_user(test_db):
    """A user whose CSP org is active, with a CspProfile — what /csp/* requires.

    Note `CspOrganisation` is its own table, distinct from the general
    `Organisation`: `csp_profiles.organisation_id` FKs to `csp_organisations`.
    The subscription must be 'active' or the auth adapter 402s every endpoint.
    """
    user = make_user(test_db, plan="csp", role="VENDOR")
    org = CspOrganisation(
        id=uuid.uuid4(),
        name="Test CSP Pte Ltd",
        owner_user_id=user.id,
        subscription_status="active",
    )
    test_db.add(org)
    test_db.flush()
    # The adapter resolves the org via membership, not ownership — without this
    # row it provisions a second, inactive org and every call 402s.
    test_db.add(CspOrgMembership(org_id=org.id, user_id=user.id, role="csp_admin"))
    test_db.add(CspProfile(
        id=uuid.uuid4(),
        organisation_id=org.id,
        legal_name="Test CSP Pte Ltd",
        uen="202312345A",
    ))
    test_db.commit()
    user._csp_org_id = org.id
    return user


def _programme(test_db, org_id, schema_version):
    profile = test_db.query(CspProfile).filter(
        CspProfile.organisation_id == org_id
    ).first()
    row = CspAmlProgramme(
        id=uuid.uuid4(),
        csp_id=profile.id,
        version=1,
        is_current=True,
        status="draft",
        schema_version=schema_version,
        generated_at=datetime.now(timezone.utc),
    )
    test_db.add(row)
    test_db.commit()
    return row


def _org_id(test_db, user):
    return user._csp_org_id


# ── Documents ────────────────────────────────────────────────────────────────

def test_unstamped_pack_reads_as_outdated(client, test_db, csp_user):
    """NULL schema_version predates the constant — those packs cite s.48A."""
    _programme(test_db, _org_id(test_db, csp_user), None)

    resp = client.get("/api/v1/csp/documents", headers=auth_headers(csp_user))
    assert resp.status_code == 200, resp.text
    doc = resp.json()[0]
    assert doc["schema_version"] is None
    assert doc["outdated"] is True
    assert doc["outdated_reason"]


def test_current_pack_is_not_flagged(client, test_db, csp_user):
    _programme(test_db, _org_id(test_db, csp_user), CSP_DOC_SCHEMA_VERSION)

    resp = client.get("/api/v1/csp/documents", headers=auth_headers(csp_user))
    assert resp.status_code == 200, resp.text
    doc = resp.json()[0]
    assert doc["outdated"] is False
    assert doc["outdated_reason"] is None


def test_regenerate_is_refused_when_pack_is_already_current(client, test_db, csp_user):
    """Cost gate: 8 DeepSeek completions per run, so this must not be free-form."""
    _programme(test_db, _org_id(test_db, csp_user), CSP_DOC_SCHEMA_VERSION)

    resp = client.post("/api/v1/csp/documents/regenerate", headers=auth_headers(csp_user))
    assert resp.status_code == 409, resp.text


def test_regenerate_is_refused_when_no_pack_exists(client, test_db, csp_user):
    resp = client.post("/api/v1/csp/documents/regenerate", headers=auth_headers(csp_user))
    assert resp.status_code == 404, resp.text


def test_outdated_pack_queues_a_free_regeneration(client, test_db, csp_user, monkeypatch):
    _programme(test_db, _org_id(test_db, csp_user), None)

    queued: dict = {}

    class _Task:
        id = "task-123"

    def _fake_apply_async(args=None, **kw):
        queued["args"] = args
        return _Task()

    from app.workers import csp_tasks
    monkeypatch.setattr(csp_tasks.generate_csp_documents, "apply_async", _fake_apply_async)

    resp = client.post("/api/v1/csp/documents/regenerate", headers=auth_headers(csp_user))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    assert body["to_schema_version"] == CSP_DOC_SCHEMA_VERSION
    assert queued["args"], "the regeneration task was never queued"


# ── Baseline ─────────────────────────────────────────────────────────────────

def _baseline(test_db, user, snapshot):
    row = Report(
        id=uuid.uuid4(),
        owner_id=user.id,
        framework="csp_baseline",
        company_name="Test CSP Pte Ltd",
        assessment_data=snapshot,
        status="completed",
        s3_url="https://example.invalid/baseline.pdf",
        file_key="csp/baseline/test.pdf",
        completed_at=datetime.now(timezone.utc),
    )
    test_db.add(row)
    test_db.commit()
    return row


def test_unstamped_baseline_reads_as_outdated(client, test_db, csp_user):
    _baseline(test_db, csp_user, {"plan_label": "CSP Compliance Pack — Full"})

    resp = client.get("/api/v1/csp/baseline/latest", headers=auth_headers(csp_user))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["available"] is True
    assert body["outdated"] is True
    assert body["outdated_reason"]


def test_current_baseline_is_not_flagged(client, test_db, csp_user):
    _baseline(test_db, csp_user, {"schema_version": CSP_BASELINE_SCHEMA_VERSION})

    resp = client.get("/api/v1/csp/baseline/latest", headers=auth_headers(csp_user))
    assert resp.status_code == 200, resp.text
    assert resp.json()["outdated"] is False


def test_baseline_reissue_replays_the_original_generation_inputs(
    client, test_db, csp_user, monkeypatch
):
    """The reissue must name the entity the original named.

    Re-deriving instead of replaying falls back to the account's own company,
    which is wrong whenever the baseline was bought about a managed client
    entity — the firm would end up certifying itself under its client's order.
    """
    entity_id = str(uuid.uuid4())
    _baseline(test_db, csp_user, {
        "plan": "csp_monitoring",
        "billing_type": "monthly",
        "override_company": "Client Alpha Pte Ltd",
        "override_uen": "201911111B",
        "managed_entity_id": entity_id,
    })

    captured: dict = {}

    class _Task:
        id = "task-456"

    def _fake_apply_async(kwargs=None, **kw):
        captured.update(kwargs or {})
        return _Task()

    from app.workers import csp_tasks
    monkeypatch.setattr(
        csp_tasks.run_csp_baseline_for_user, "apply_async", _fake_apply_async
    )

    resp = client.post("/api/v1/csp/baseline/regenerate", headers=auth_headers(csp_user))
    assert resp.status_code == 200, resp.text

    assert captured["plan"] == "csp_monitoring"
    assert captured["billing_type"] == "monthly"
    assert captured["override_company"] == "Client Alpha Pte Ltd"
    assert captured["managed_entity_id"] == entity_id
    # Without this the 24h once-only send lock swallows the reissue and the
    # customer is told it was queued while nothing is delivered.
    assert captured["bypass_idempotency"] is True


def test_baseline_reissue_is_refused_when_already_current(client, test_db, csp_user):
    _baseline(test_db, csp_user, {"schema_version": CSP_BASELINE_SCHEMA_VERSION})

    resp = client.post("/api/v1/csp/baseline/regenerate", headers=auth_headers(csp_user))
    assert resp.status_code == 409, resp.text
