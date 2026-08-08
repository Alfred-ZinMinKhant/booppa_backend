"""Regression coverage for the `checkout/verify` brief-recovery branch.

The bug this locks down: `checkout_verify` computed `requires_brief` from a
five-SKU set, but its lazy-recovery branch tested only {"rfp_complete",
"rfp_express"}. When the webhook had not yet created (or had failed to create)
a `PendingRfpIntake`, a buyer of `rfp_accelerator`, `enterprise_bid_kit` or
`compliance_evidence_pack` — the three most expensive SKUs — got
`requires_brief=True` with `pending_rfp_intake_id=None`. The success page then
had no brief CTA and no kit, so the purchase dead-ended after payment.

Both sides now derive from `app/services/fulfillment/catalog.py`.

The second guarantee here is subtler and would not be caught by simply widening
the set: the recovered row must carry the *kit* type in `rfp_product_type`
(`rfp_express` / `rfp_complete`) and the *bundle* name in `bundle_source`.
Writing "rfp_accelerator" into `rfp_product_type` produces a row that
`fulfill_rfp_task` cannot fulfil — a silent second dead-end.
"""
import uuid
from unittest.mock import MagicMock

import pytest

from app.services.fulfillment.catalog import (
    BUNDLE_COMPONENTS,
    RFP_BRIEF_PRODUCT_TYPES,
    RFP_PRODUCT_TYPES,
    rfp_component_for,
)


def _seed_user(db):
    from app.core.models import User

    u = User(
        email=f"brief+{uuid.uuid4().hex[:8]}@booppa.io",
        hashed_password="x",
        role="VENDOR",
        company="Acme Pte Ltd",
        website="https://acme.example",
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _stub_stripe(mocker, *, email, product_type):
    """A paid Checkout session carrying product_type metadata."""
    session = {
        "payment_status": "paid",
        "payment_intent": {"status": "succeeded"},
        "metadata": {"product_type": product_type},
        "customer_details": {"email": email},
    }
    client = MagicMock()
    client.checkout.Session.retrieve.return_value = session
    mocker.patch("app.api.stripe_checkout.get_stripe_client", return_value=client)
    return client


# ── The catalogue itself ────────────────────────────────────────────────────

def test_brief_set_is_derived_not_hand_listed():
    """Every SKU with an `rfp` component must owe a brief, and nothing else."""
    expected = {"rfp_express", "rfp_complete", "rfp_accelerator",
                "enterprise_bid_kit", "compliance_evidence_pack"}
    assert RFP_BRIEF_PRODUCT_TYPES == expected
    # vendor_trust_pack has no RFP component and must never demand a brief.
    assert "vendor_trust_pack" not in RFP_BRIEF_PRODUCT_TYPES


def test_bundle_names_are_never_kit_types():
    """`rfp_product_type` may only ever hold a standalone kit type."""
    for bundle in BUNDLE_COMPONENTS:
        assert rfp_component_for(bundle) in RFP_PRODUCT_TYPES | {None}
        assert rfp_component_for(bundle) != bundle or bundle in RFP_PRODUCT_TYPES


# ── The recovery branch, per SKU ────────────────────────────────────────────

@pytest.mark.parametrize("product_type", sorted(RFP_BRIEF_PRODUCT_TYPES))
def test_verify_recovers_missing_intake_for_every_brief_sku(
    product_type, client, test_db, mocker
):
    """No intake row exists -> verify must lazily create one and surface its id.

    Before the fix this passed for the two standalone kits and returned
    pending_rfp_intake_id=None for the three bundles.
    """
    from app.core.models import PendingRfpIntake

    user = _seed_user(test_db)
    _stub_stripe(mocker, email=user.email, product_type=product_type)

    resp = client.get(f"/api/stripe/checkout/verify?session_id=cs_test_{uuid.uuid4().hex[:12]}")
    assert resp.status_code == 200
    body = resp.json()

    assert body["requires_brief"] is True
    assert body["brief_satisfied"] is False
    assert body["pending_rfp_intake_id"], (
        f"{product_type} paid but no brief CTA — buyer dead-ends on the success page"
    )

    row = (
        test_db.query(PendingRfpIntake)
        .filter(PendingRfpIntake.id == body["pending_rfp_intake_id"])
        .first()
    )
    assert row is not None
    # The kit type, not the bundle name — otherwise fulfill_rfp_task can't fulfil it.
    assert row.rfp_product_type == rfp_component_for(product_type)
    assert row.rfp_product_type in RFP_PRODUCT_TYPES
    assert row.bundle_source == product_type
    assert row.status == "pending"


def test_verify_does_not_invent_a_brief_for_non_rfp_products(client, test_db, mocker):
    """A SKU with no RFP component must never grow an intake row."""
    from app.core.models import PendingRfpIntake

    user = _seed_user(test_db)
    _stub_stripe(mocker, email=user.email, product_type="vendor_trust_pack")

    resp = client.get(f"/api/stripe/checkout/verify?session_id=cs_test_{uuid.uuid4().hex[:12]}")
    assert resp.status_code == 200
    body = resp.json()

    assert body["requires_brief"] is False
    assert body["brief_satisfied"] is True
    assert not body.get("pending_rfp_intake_id")
    assert test_db.query(PendingRfpIntake).count() == 0


def test_verify_reuses_the_session_row_instead_of_stacking_a_second(
    client, test_db, mocker
):
    """An existing pending row for THIS session is surfaced, not duplicated."""
    from app.core.models import PendingRfpIntake

    user = _seed_user(test_db)
    session_id = f"cs_test_{uuid.uuid4().hex[:12]}"
    existing = PendingRfpIntake(
        user_id=user.id,
        session_id=session_id,
        rfp_product_type="rfp_complete",
        bundle_source="enterprise_bid_kit",
        status="pending",
    )
    test_db.add(existing)
    test_db.commit()
    test_db.refresh(existing)

    _stub_stripe(mocker, email=user.email, product_type="enterprise_bid_kit")
    resp = client.get(f"/api/stripe/checkout/verify?session_id={session_id}")

    assert resp.status_code == 200
    assert resp.json()["pending_rfp_intake_id"] == str(existing.id)
    assert test_db.query(PendingRfpIntake).count() == 1


def test_submitted_brief_for_this_session_satisfies_the_gate(client, test_db, mocker):
    """status='submitted' on THIS session -> brief_satisfied, no CTA."""
    from app.core.models import PendingRfpIntake

    user = _seed_user(test_db)
    session_id = f"cs_test_{uuid.uuid4().hex[:12]}"
    test_db.add(
        PendingRfpIntake(
            user_id=user.id,
            session_id=session_id,
            rfp_product_type="rfp_express",
            bundle_source="rfp_accelerator",
            status="submitted",
        )
    )
    test_db.commit()

    _stub_stripe(mocker, email=user.email, product_type="rfp_accelerator")
    resp = client.get(f"/api/stripe/checkout/verify?session_id={session_id}")

    body = resp.json()
    assert body["brief_satisfied"] is True
    assert not body.get("pending_rfp_intake_id")


def test_submitted_row_from_a_prior_cycle_does_not_satisfy_this_purchase(
    client, test_db, mocker
):
    """The regression CLAUDE.md warns about: an older `submitted` row must not
    win the created_at race and falsely report brief_satisfied for a NEW session,
    which strands the buyer on 'Generating…' forever."""
    from app.core.models import PendingRfpIntake

    user = _seed_user(test_db)
    test_db.add(
        PendingRfpIntake(
            user_id=user.id,
            session_id="cs_test_previous_cycle",
            rfp_product_type="rfp_complete",
            bundle_source="compliance_evidence_pack",
            status="submitted",
        )
    )
    test_db.commit()

    new_session = f"cs_test_{uuid.uuid4().hex[:12]}"
    _stub_stripe(mocker, email=user.email, product_type="compliance_evidence_pack")
    resp = client.get(f"/api/stripe/checkout/verify?session_id={new_session}")

    body = resp.json()
    assert body["brief_satisfied"] is False
    assert body["pending_rfp_intake_id"]
    row = (
        test_db.query(PendingRfpIntake)
        .filter(PendingRfpIntake.session_id == new_session)
        .first()
    )
    assert row is not None and row.status == "pending"
