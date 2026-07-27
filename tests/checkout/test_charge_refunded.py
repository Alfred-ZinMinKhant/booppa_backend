"""`charge.refunded` bookkeeping.

`refund_service` records the refunds it issues itself. This webhook branch exists
only for the ones it never sees — a human clicking Refund in the Stripe
dashboard. Without it the DB keeps reporting a paid, unrefunded order for money
we have already given back, and `/admin/stranded-fulfillments` misreads the row.

A charge carries a `payment_intent`, not a session, so the handler has to walk
back PI → Checkout Session → `assessment_data["stripe_session_id"]`. That hop is
what most of these tests pin.
"""
import time
import uuid

import pytest

from tests._test_helpers import make_user


def _charge_event(pi_id="pi_refund_1", *, amount_refunded=4900, refund_id="re_dash"):
    return {
        "id": f"evt_test_{uuid.uuid4().hex[:24]}",
        "object": "event",
        "type": "charge.refunded",
        "created": int(time.time()),
        "livemode": False,
        "data": {
            "object": {
                "id": "ch_1",
                "payment_intent": pi_id,
                "amount_refunded": amount_refunded,
                "currency": "sgd",
                "refunds": {"data": [{"id": refund_id}]},
            }
        },
    }


@pytest.fixture
def report(test_db):
    from app.core.models import Report

    user = make_user(test_db, email="refunded@booppa.io", company="Acme")
    rep = Report(
        owner_id=user.id,
        framework="pdpa_quick_scan",
        company_name="Acme",
        status="site_inaccessible",
        assessment_data={
            "payment_confirmed": True,
            "stripe_session_id": "cs_test_refunded",
        },
    )
    test_db.add(rep)
    test_db.commit()
    return rep


@pytest.fixture
def session_lookup(mocker):
    """Stripe's PI → Checkout Session hop."""
    import app.api.stripe_webhook as wh

    return mocker.patch.object(
        wh.stripe.checkout.Session,
        "list",
        return_value={"data": [{"id": "cs_test_refunded"}]},
    )


def test_dashboard_refund_is_recorded_on_the_report(
    post_webhook, test_db, report, session_lookup
):
    resp = post_webhook(_charge_event())

    assert resp.status_code == 200
    test_db.expire_all()
    ad = report.assessment_data
    assert ad["refunded"] is True
    assert ad["refund_id"] == "re_dash"
    assert ad["refund_amount"] == 4900
    # Attributed, so it is distinguishable from one our own code issued.
    assert ad["refund_source"] == "stripe_dashboard"


def test_the_recorded_amount_is_stripes_not_assumed_full(
    post_webhook, test_db, report, session_lookup
):
    """A partial refund must not be written down as a full one."""
    post_webhook(_charge_event(amount_refunded=1000))

    test_db.expire_all()
    assert report.assessment_data["refund_amount"] == 1000


def test_an_unmatched_payment_intent_is_not_fatal(post_webhook, test_db, mocker):
    """No session for the PI (e.g. an invoice charge) — log and move on."""
    import app.api.stripe_webhook as wh

    mocker.patch.object(wh.stripe.checkout.Session, "list", return_value={"data": []})

    assert post_webhook(_charge_event(pi_id="pi_orphan")).status_code == 200


def test_a_stripe_failure_does_not_fail_the_webhook(post_webhook, mocker):
    """A 500 here makes Stripe redeliver the whole event, re-running the
    fulfillment branches above this one. Bookkeeping must never cause that."""
    import app.api.stripe_webhook as wh

    mocker.patch.object(
        wh.stripe.checkout.Session, "list", side_effect=Exception("stripe down")
    )

    assert post_webhook(_charge_event()).status_code == 200
