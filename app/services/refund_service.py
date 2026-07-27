"""Stripe refunds.

Introduced after incident 2026-07-27: a paid PDPA Quick Scan whose target site
sat behind a WAF ended in the terminal ``site_inaccessible`` state. No score
could ever be produced, no deliverable was sent, and — because the codebase had
no refund path at all — we simply kept the money.

Design notes worth keeping in mind before editing:

* **We store no payment_intent.** There is no Purchase/Order table; the only
  durable handle is ``Report.assessment_data["stripe_session_id"]``. The
  PaymentIntent is therefore resolved by retrieving the Checkout Session from
  Stripe, the same way ``app/api/reports.py`` and ``app/api/notarize.py`` do.
* **Refunds move real money and run inside a Celery task that retries.** Every
  entry point must be idempotent, and the guard cannot rely on Redis being up —
  hence BOTH a cache claim and a Stripe ``idempotency_key``.
* **Admin test checkouts must never reach Stripe.** ``admin-sim-*`` sessions do
  not exist there; calling refund on one raises and would mask real failures.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import stripe

from app.core.config import settings
from app.core.demo_flags import is_demo_session

logger = logging.getLogger(__name__)

# A refund claim outlives any plausible Celery retry chain. It is deliberately
# long: the failure mode we are protecting against (refunding twice) is far more
# damaging than the one it risks (a legitimate second refund needing the admin
# endpoint).
_REFUND_CLAIM_TTL = 30 * 24 * 3600


def _claim(session_id: str) -> tuple[bool, object | None]:
    """Atomically claim the right to refund `session_id`.

    Returns (won, cache_module). Fails OPEN — if the cache is unreachable we
    proceed, because Stripe's own idempotency_key is the real backstop and a
    silently skipped refund is worse than a redundant API call.
    """
    try:
        from app.core.cache import cache

        key = cache.cache_key(f"refund:{session_id}")
        return (cache.add(key, {"claimed": True}, ttl=_REFUND_CLAIM_TTL), cache)
    except Exception as e:
        logger.warning(f"[Refund] claim unavailable for {session_id}, proceeding: {e}")
        return (True, None)


def _release(cache, session_id: str) -> None:
    """Drop the claim so a genuine retry can re-attempt a refund that failed."""
    if cache is None:
        return
    try:
        cache.delete(cache.cache_key(f"refund:{session_id}"))
    except Exception:
        pass


def refund_for_session(session_id: str | None, *, reason: str) -> dict:
    """Issue a full refund for a Checkout Session. Never raises.

    Returns a dict the caller can persist and show to the buyer::

        {"refunded": bool, "refund_id": str|None, "amount": int|None,
         "currency": str|None, "skipped_reason": str|None}

    ``amount`` is in the currency's minor unit (Stripe's convention), so 4900 is
    SGD 49.00.
    """
    result = {
        "refunded": False,
        "refund_id": None,
        "amount": None,
        "currency": None,
        "skipped_reason": None,
    }

    if not session_id:
        result["skipped_reason"] = "no_session_id"
        return result

    # Admin test checkout — these sessions do not exist on Stripe.
    if is_demo_session(session_id):
        logger.info(f"[Refund] {session_id} is an admin simulation — skipping Stripe")
        result["skipped_reason"] = "demo"
        return result

    secret = settings.STRIPE_SECRET_KEY
    if not secret:
        logger.error(f"[Refund] No STRIPE_SECRET_KEY configured; cannot refund {session_id}")
        result["skipped_reason"] = "no_stripe_key"
        return result

    won, cache = _claim(session_id)
    if not won:
        logger.info(f"[Refund] {session_id} already claimed — not refunding twice")
        result["skipped_reason"] = "already_claimed"
        return result

    try:
        stripe.api_key = secret
        session = stripe.checkout.Session.retrieve(session_id, expand=["payment_intent"])

        pi = session.get("payment_intent") if isinstance(session, dict) else getattr(session, "payment_intent", None)
        pi_id = pi.get("id") if isinstance(pi, dict) else (getattr(pi, "id", None) or pi)
        if not pi_id:
            # Nothing was captured (e.g. an unpaid/expired session) — there is
            # no money to return, which is a clean outcome, not a failure.
            logger.info(f"[Refund] {session_id} has no payment_intent — nothing to refund")
            result["skipped_reason"] = "no_payment_intent"
            _release(cache, session_id)
            return result

        # Already refunded (e.g. a human did it in the Stripe dashboard first).
        try:
            existing = stripe.Refund.list(payment_intent=pi_id, limit=1)
            rows = existing.get("data") if isinstance(existing, dict) else getattr(existing, "data", [])
            if rows:
                prior = rows[0]
                prior_id = prior.get("id") if isinstance(prior, dict) else getattr(prior, "id", None)
                logger.info(f"[Refund] {session_id} already refunded upstream ({prior_id})")
                result.update(refunded=True, refund_id=prior_id, skipped_reason="already_refunded")
                return result
        except Exception as e:
            # Not fatal: the idempotency_key below still prevents a double.
            logger.warning(f"[Refund] Could not list existing refunds for {pi_id}: {e}")

        refund = stripe.Refund.create(
            payment_intent=pi_id,
            reason="requested_by_customer",
            metadata={"booppa_reason": (reason or "")[:500], "session_id": session_id},
            # Survives a Redis outage: Stripe itself collapses duplicates.
            idempotency_key=f"booppa-refund-{session_id}",
        )

        result.update(
            refunded=True,
            refund_id=refund.get("id") if isinstance(refund, dict) else getattr(refund, "id", None),
            amount=refund.get("amount") if isinstance(refund, dict) else getattr(refund, "amount", None),
            currency=refund.get("currency") if isinstance(refund, dict) else getattr(refund, "currency", None),
        )
        logger.info(
            f"[Refund] Refunded {result['amount']} {result['currency']} for {session_id} "
            f"(refund={result['refund_id']}, reason={reason})"
        )
        return result

    except Exception as e:
        # Release the claim so a retry — or the admin endpoint — can try again.
        _release(cache, session_id)
        logger.error(f"[Refund] FAILED for {session_id}: {e}")
        result["skipped_reason"] = f"error:{str(e)[:200]}"
        return result


def refund_summary_for_assessment(result: dict) -> dict:
    """Shape a refund result for persistence into ``Report.assessment_data``."""
    return {
        "refund_attempted_at": datetime.now(timezone.utc).isoformat(),
        "refunded": bool(result.get("refunded")),
        "refund_id": result.get("refund_id"),
        "refund_amount": result.get("amount"),
        "refund_currency": result.get("currency"),
        "refund_skipped_reason": result.get("skipped_reason"),
    }
