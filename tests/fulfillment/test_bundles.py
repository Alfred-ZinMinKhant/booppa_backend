"""Bundle fulfillment: fan-out shape via BUNDLE_COMPONENTS."""
import pytest

from tests.fixtures.product_catalog import BUNDLES, sku_id
from tests.fixtures.stripe_events import wrap_event


@pytest.mark.parametrize("case", BUNDLES, ids=[sku_id(c) for c in BUNDLES])
def test_bundle_queues_fulfill_bundle_task_with_components(
    case, client, post_webhook, stripe_session_factory, mocker
):
    """Each bundle should be passed verbatim to fulfill_bundle_task.delay."""
    fake_task = mocker.patch("app.workers.tasks.fulfill_bundle_task")
    fake_task.delay = mocker.MagicMock()

    session = stripe_session_factory(case.product_type)
    resp = post_webhook(wrap_event(session))
    assert resp.status_code == 200

    fake_task.delay.assert_called_once()
    kwargs = fake_task.delay.call_args.kwargs
    assert kwargs["product_type"] == case.product_type
    assert kwargs["customer_email"] == session["customer_email"]
    assert kwargs["session_id"] == session["id"]
    md = kwargs["metadata"]
    assert md["company_name"] == "Test Co"
    assert md["vendor_url"] == "https://example.test"


def test_bundle_component_definitions_intact():
    """Sanity: BUNDLE_COMPONENTS contents match the product spec.

    These numbers come from the canonical product spec in memory:
      - vendor_trust_pack: VP + PDPA + 2× notarization
      - rfp_accelerator:    VP + PDPA + 2× notarization + rfp_express
      - enterprise_bid_kit: VP + PDPA + 7× notarization + rfp_complete
      - compliance_evidence_pack: PDPA + 1× notarization + rfp_complete + cover_sheet
    """
    from app.api.stripe_webhook import BUNDLE_COMPONENTS

    vtp = BUNDLE_COMPONENTS["vendor_trust_pack"]
    assert vtp["vendor_proof"] and vtp["pdpa"] and vtp["notarization_count"] == 2
    assert vtp["rfp"] is None

    acc = BUNDLE_COMPONENTS["rfp_accelerator"]
    assert acc["rfp"] == "rfp_express" and acc["notarization_count"] == 2

    ebk = BUNDLE_COMPONENTS["enterprise_bid_kit"]
    assert ebk["rfp"] == "rfp_complete" and ebk["notarization_count"] == 7

    cep = BUNDLE_COMPONENTS["compliance_evidence_pack"]
    assert cep.get("cover_sheet") is True and cep["rfp"] == "rfp_complete"
