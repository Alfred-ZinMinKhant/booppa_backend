"""Stripe live/test mode consistency.

Production has run entirely on `sk_test_` since launch (AUDIT_2026-08-08.md P0-D),
which nothing detected because *every* observable signal looks identical in test
mode: checkout completes, the webhook fires, entitlement is granted, the kit
generates, the email sends. Only the money is missing.

Staying in test mode is a legitimate choice. Drifting into a half-migrated state
is not, and that is the real risk in the switch — the credentials live in three
places (`booppa/app-secrets`, the `STRIPE_*` price IDs in `ci.yml`, and the
webhook signing secret) and moving two of the three produces failures that are
either silent or wildly misleading:

  * live secret + test prices → every checkout 400s on "No such price"
  * live secret + test webhook secret → payments succeed, signature
    verification fails, NOTHING is ever fulfilled and the customer is charged
  * test secret + live prices → the same, mirrored

So this module does two things: it derives the actual mode from the credentials
themselves, and it refuses to let the three disagree.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

LIVE = "live"
TEST = "test"
UNKNOWN = "unknown"


def _mode_of(value: str | None) -> str:
    """Stripe encodes the mode in the credential itself; trust that, not a flag.

    Applies to `sk_`/`pk_`/`rk_` keys and to `price_`/`prod_` ids, which carry no
    mode marker at all — hence UNKNOWN for those, resolved by live lookup only.
    """
    v = (value or "").strip()
    if not v:
        return UNKNOWN
    if "_test_" in v or v.startswith(("sk_test", "pk_test", "rk_test", "whsec_test")):
        return TEST
    if "_live_" in v or v.startswith(("sk_live", "pk_live", "rk_live")):
        return LIVE
    return UNKNOWN


def secret_key_mode() -> str:
    return _mode_of(os.environ.get("STRIPE_SECRET_KEY"))


def declared_mode() -> str:
    """What the operator says they intend, via `STRIPE_MODE`.

    Unset means "whatever the key says" — this is deliberately not required, so
    an existing deployment does not break on rollout. Setting it turns an
    accidental mode change into a startup failure instead of a silent one.
    """
    return (os.environ.get("STRIPE_MODE") or "").strip().lower() or UNKNOWN


def check_consistency() -> list[str]:
    """Return a list of problems. Empty means the Stripe config hangs together.

    Deliberately returns rather than raises: the caller decides whether a given
    problem is fatal, and a webhook-secret mismatch in a dev shell should not
    stop the app booting.
    """
    problems: list[str] = []
    key_mode = secret_key_mode()

    if key_mode == UNKNOWN:
        problems.append(
            "STRIPE_SECRET_KEY is unset or unrecognised — no checkout can be created"
        )
        return problems

    intent = declared_mode()
    if intent in (LIVE, TEST) and intent != key_mode:
        problems.append(
            f"STRIPE_MODE says {intent!r} but STRIPE_SECRET_KEY is a {key_mode} key"
        )

    # The webhook secret is the half of the pair that silently breaks fulfilment:
    # Stripe accepts the payment, we reject the notification, and the customer is
    # charged for something no code ever delivers.
    whsec = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not whsec:
        problems.append(
            "STRIPE_WEBHOOK_SECRET is unset — every webhook fails signature "
            "verification, so paid orders are never fulfilled"
        )

    return problems


def price_ids_for_audit() -> dict[str, str]:
    """Every configured `STRIPE_*` price id, for the go-live diff."""
    return {
        k: v
        for k, v in os.environ.items()
        if k.startswith(("STRIPE_", "NEXT_PUBLIC_STRIPE_"))
        and v.startswith(("price_", "prod_"))
    }


def log_startup_banner() -> None:
    """Say the mode out loud at boot, every boot.

    The whole reason test mode survived in production is that nothing ever
    stated it. One unmissable log line at startup is the cheapest possible
    guard against the same thing happening in reverse.
    """
    key_mode = secret_key_mode()
    n_prices = len(price_ids_for_audit())
    problems = check_consistency()

    if key_mode == LIVE:
        logger.warning(
            "[Stripe] LIVE MODE — real cards will be charged (%d price ids configured)",
            n_prices,
        )
    else:
        logger.warning(
            "[Stripe] %s MODE — no real money will move (%d price ids configured)",
            key_mode.upper(),
            n_prices,
        )

    for problem in problems:
        logger.error("[Stripe] CONFIG PROBLEM: %s", problem)
