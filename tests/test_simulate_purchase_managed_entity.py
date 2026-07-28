"""Test checkout must exercise the managed-entity path, not simulate around it.

The admin test checkout is how identity behaviour actually gets verified before a
release. If it resolves identity differently from a real Stripe order — or worse,
if it stamps the subject company onto the QA account the way the old flow did —
then a passing test run says nothing about production.

Two invariants here:
  * an entity-subject run never writes the subject's company/website onto the
    ordering account (the SPQR write, reproduced in miniature), and
  * the order carries `managed_entity_id` in its metadata, so fulfillment re-reads
    the row instead of trusting a name string.
"""
from __future__ import annotations

import uuid

import pytest

from app.api.admin import SimulatePurchaseRequest, simulate_purchase
from app.core.models import ManagedEntity, User


CAPTURED: dict = {}


@pytest.fixture
def stub_fulfillment(monkeypatch):
    """Capture the metadata a run would hand to fulfillment, and send no mail."""
    import app.services.fulfillment as fulfillment

    async def _capture(*, product_type, customer_email, metadata, session_id, **kw):
        CAPTURED.clear()
        CAPTURED.update(metadata)
        return None

    monkeypatch.setattr(fulfillment, "fulfill_standalone_no_report", _capture)
    return CAPTURED


@pytest.fixture
def acra_confirms(monkeypatch):
    """ACRA answers with a registered name + UEN for whatever it is asked about."""
    import app.services.evidence_enricher as enricher

    async def _match(db, uen, company, website=None):
        return ("ALPHA HOLDINGS PTE. LTD.", "201234567A")

    monkeypatch.setattr(enricher, "_match_acra", _match)


@pytest.fixture
def qa_account(test_db):
    email = f"sim-{uuid.uuid4().hex[:10]}@booppa.io"
    user = User(
        id=uuid.uuid4(),
        email=email,
        hashed_password="x",
        role="VENDOR",
        company="BOOPPA CORPORATE SERVICES PTE. LTD.",
        website="https://booppa.io",
        is_active=True,
    )
    test_db.add(user)
    test_db.commit()
    return user


@pytest.mark.asyncio
async def test_entity_run_does_not_stamp_the_subject_onto_the_account(
    test_db, qa_account, stub_fulfillment, acra_confirms
):
    """The whole point. The QA account keeps its own identity; the client company
    goes to the entity row."""
    result = await simulate_purchase(
        SimulatePurchaseRequest(
            product_type="pdpa_quick_scan",
            customer_email=qa_account.email,
            company_name="ALPHA HOLDINGS PTE. LTD.",
            vendor_url="https://alpha.example.com",
            managed_entity_name="ALPHA HOLDINGS PTE. LTD.",
        ),
        _auth=True,
    )

    test_db.refresh(qa_account)
    assert qa_account.company == "BOOPPA CORPORATE SERVICES PTE. LTD.", (
        "the subject company was written onto the ordering account — this is the "
        "exact write the per-report subject architecture exists to remove"
    )
    assert qa_account.website == "https://booppa.io"

    entity = (
        test_db.query(ManagedEntity)
        .filter(ManagedEntity.owner_user_id == qa_account.id)
        .one()
    )
    assert entity.company_name == "ALPHA HOLDINGS PTE. LTD."
    assert entity.uen == "201234567A"
    assert entity.verified_at is not None, "a confirmed match must leave provenance"

    assert result["subject_identity"]["verified"] is True
    assert result["subject_identity"]["managed_entity_id"] == str(entity.id)


@pytest.mark.asyncio
async def test_entity_id_flows_into_fulfillment_metadata(
    test_db, qa_account, stub_fulfillment, acra_confirms
):
    """Fulfillment must be able to re-read the row. A company name in metadata is
    not enough — that is a string anyone can collide with."""
    await simulate_purchase(
        SimulatePurchaseRequest(
            product_type="pdpa_quick_scan",
            customer_email=qa_account.email,
            managed_entity_name="ALPHA HOLDINGS PTE. LTD.",
            vendor_url="https://alpha.example.com",
        ),
        _auth=True,
    )

    entity = (
        test_db.query(ManagedEntity)
        .filter(ManagedEntity.owner_user_id == qa_account.id)
        .one()
    )
    assert stub_fulfillment.get("managed_entity_id") == str(entity.id)


@pytest.mark.asyncio
async def test_unknown_entity_id_is_refused_rather_than_invented(
    test_db, qa_account, stub_fulfillment, acra_confirms
):
    """An id that resolves to nothing must fail loudly. Registering a row for it
    would make the test tool pass on precisely the case a real checkout 422s."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await simulate_purchase(
            SimulatePurchaseRequest(
                product_type="pdpa_quick_scan",
                customer_email=qa_account.email,
                managed_entity_id=str(uuid.uuid4()),
            ),
            _auth=True,
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_run_without_an_entity_still_updates_the_account(
    test_db, qa_account, stub_fulfillment, acra_confirms
):
    """The self-assessment case is untouched: no entity named means the account is
    its own subject, and the active test identity still drives it."""
    await simulate_purchase(
        SimulatePurchaseRequest(
            product_type="pdpa_quick_scan",
            customer_email=qa_account.email,
            company_name="Suite Test Co A",
            vendor_url="https://suite-a.booppa.io",
        ),
        _auth=True,
    )

    test_db.refresh(qa_account)
    assert qa_account.company == "Suite Test Co A"
    assert qa_account.website == "https://suite-a.booppa.io"
    assert "managed_entity_id" not in stub_fulfillment
