"""Refunds for scans we could not perform.

Introduced with incident 2026-07-27: a paid PDPA Quick Scan whose target sat
behind a WAF could never produce a score, and the codebase had no refund path at
all — we kept the money.

This moves real money from inside a Celery task that retries, so the tests below
are mostly about the ways it must refuse to fire twice.
"""
import pytest

from app.services import refund_service


@pytest.fixture
def stripe_mock(mocker):
    """Replace the stripe module inside refund_service."""
    m = mocker.patch.object(refund_service, "stripe")
    m.checkout.Session.retrieve.return_value = {"payment_intent": {"id": "pi_123"}}
    m.Refund.list.return_value = {"data": []}
    m.Refund.create.return_value = {
        "id": "re_123",
        "amount": 4900,
        "currency": "sgd",
    }
    return m


@pytest.fixture
def key(mocker):
    mocker.patch.object(refund_service.settings, "STRIPE_SECRET_KEY", "sk_test_x")


@pytest.fixture
def fresh_claim(mocker):
    """Every test gets an unclaimed session unless it says otherwise."""
    return mocker.patch.object(
        refund_service, "_claim", return_value=(True, None)
    )


# ── Refuses to touch Stripe ──────────────────────────────────────────────────

def test_admin_simulation_never_reaches_stripe(stripe_mock, key, fresh_claim):
    """admin-sim-* sessions do not exist on Stripe; refunding one would raise."""
    out = refund_service.refund_for_session("admin-sim-abc123", reason="unscannable")

    assert out["refunded"] is False
    assert out["skipped_reason"] == "demo"
    stripe_mock.checkout.Session.retrieve.assert_not_called()
    stripe_mock.Refund.create.assert_not_called()


def test_missing_session_id_is_a_clean_skip(stripe_mock, key, fresh_claim):
    out = refund_service.refund_for_session(None, reason="unscannable")

    assert out["skipped_reason"] == "no_session_id"
    stripe_mock.Refund.create.assert_not_called()


def test_no_stripe_key_does_not_pretend_to_refund(stripe_mock, mocker, fresh_claim):
    mocker.patch.object(refund_service.settings, "STRIPE_SECRET_KEY", "")

    out = refund_service.refund_for_session("cs_test_1", reason="unscannable")

    assert out["refunded"] is False
    assert out["skipped_reason"] == "no_stripe_key"
    stripe_mock.Refund.create.assert_not_called()


def test_unpaid_session_has_nothing_to_refund(stripe_mock, key, fresh_claim):
    stripe_mock.checkout.Session.retrieve.return_value = {"payment_intent": None}

    out = refund_service.refund_for_session("cs_test_1", reason="unscannable")

    assert out["skipped_reason"] == "no_payment_intent"
    stripe_mock.Refund.create.assert_not_called()


# ── The happy path ───────────────────────────────────────────────────────────

def test_refund_is_issued_with_an_idempotency_key(stripe_mock, key, fresh_claim):
    out = refund_service.refund_for_session("cs_test_1", reason="site 403")

    assert out["refunded"] is True
    assert out["refund_id"] == "re_123"
    assert out["amount"] == 4900
    assert out["currency"] == "sgd"

    kwargs = stripe_mock.Refund.create.call_args.kwargs
    assert kwargs["payment_intent"] == "pi_123"
    # Survives a Redis outage — Stripe collapses the duplicate itself.
    assert kwargs["idempotency_key"] == "booppa-refund-cs_test_1"


def test_payment_intent_as_bare_string_is_handled(stripe_mock, key, fresh_claim):
    """Stripe returns the PI unexpanded as a plain id in some responses."""
    stripe_mock.checkout.Session.retrieve.return_value = {"payment_intent": "pi_bare"}

    out = refund_service.refund_for_session("cs_test_1", reason="x")

    assert out["refunded"] is True
    assert stripe_mock.Refund.create.call_args.kwargs["payment_intent"] == "pi_bare"


# ── Never twice ──────────────────────────────────────────────────────────────

def test_a_lost_claim_blocks_the_refund(stripe_mock, key, mocker):
    """The cache claim is the first line of defence against a double refund."""
    mocker.patch.object(refund_service, "_claim", return_value=(False, None))

    out = refund_service.refund_for_session("cs_test_1", reason="x")

    assert out["refunded"] is False
    assert out["skipped_reason"] == "already_claimed"
    stripe_mock.Refund.create.assert_not_called()


def test_an_upstream_refund_is_not_repeated(stripe_mock, key, fresh_claim):
    """Someone refunded from the Stripe dashboard first."""
    stripe_mock.Refund.list.return_value = {"data": [{"id": "re_manual"}]}

    out = refund_service.refund_for_session("cs_test_1", reason="x")

    assert out["refunded"] is True
    assert out["refund_id"] == "re_manual"
    assert out["skipped_reason"] == "already_refunded"
    stripe_mock.Refund.create.assert_not_called()


def test_two_sequential_calls_refund_once(stripe_mock, key, mocker):
    """End-to-end through the real claim helper, with a stub cache."""
    claimed = set()

    class _Cache:
        @staticmethod
        def cache_key(v):
            return v

        @staticmethod
        def add(k, v, ttl=None):
            if k in claimed:
                return False
            claimed.add(k)
            return True

        @staticmethod
        def delete(k):
            claimed.discard(k)
            return True

    mocker.patch.dict("sys.modules", {"app.core.cache": mocker.Mock(cache=_Cache)})

    first = refund_service.refund_for_session("cs_test_1", reason="x")
    second = refund_service.refund_for_session("cs_test_1", reason="x")

    assert first["refunded"] is True
    assert second["skipped_reason"] == "already_claimed"
    assert stripe_mock.Refund.create.call_count == 1


# ── Failure handling ─────────────────────────────────────────────────────────

def test_a_stripe_error_never_raises_and_releases_the_claim(stripe_mock, key, mocker):
    """A refund failure must not crash fulfillment — the buyer email still matters."""
    release = mocker.patch.object(refund_service, "_release")
    mocker.patch.object(refund_service, "_claim", return_value=(True, "cache"))
    stripe_mock.Refund.create.side_effect = Exception("card_declined")

    out = refund_service.refund_for_session("cs_test_1", reason="x")

    assert out["refunded"] is False
    assert out["skipped_reason"].startswith("error:")
    # Released, so a retry or the admin endpoint can try again.
    release.assert_called_once()


def test_claim_failure_fails_open(stripe_mock, key, mocker):
    """If the cache is unreachable we still refund — Stripe's key is the backstop.

    A silently skipped refund is worse than a redundant API call.
    """
    mocker.patch.dict("sys.modules", {"app.core.cache": None})

    out = refund_service.refund_for_session("cs_test_1", reason="x")

    assert out["refunded"] is True


# ── Persistence shape ────────────────────────────────────────────────────────

def test_summary_shape_is_persistable():
    summary = refund_service.refund_summary_for_assessment(
        {"refunded": True, "refund_id": "re_1", "amount": 4900, "currency": "sgd"}
    )

    assert summary["refunded"] is True
    assert summary["refund_id"] == "re_1"
    assert summary["refund_amount"] == 4900
    assert summary["refund_currency"] == "sgd"
    assert "refund_attempted_at" in summary
