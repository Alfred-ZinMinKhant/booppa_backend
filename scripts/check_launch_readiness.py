#!/usr/bin/env python3
"""Everything that must be true before the first real customer.

There are no users yet, so none of the audit's findings have hurt anyone. The
risk is that they are still true on the day someone does show up — and the
recurring lesson from `AUDIT_2026-08-08.md` is that none of these announce
themselves. Production ran on Stripe test keys for months; a schema outage kept
`/api/health` green; a contact form 404'd every submission; a checkout button
posted to an endpoint that did not exist. Every one of them looked fine.

So this is the pre-launch gate: run it against the environment you are about to
put customers on. Exit 0 means the mechanical checks pass. Exit 1 means do not
launch yet.

    python scripts/check_launch_readiness.py           # code + config checks
    python scripts/check_launch_readiness.py --live    # also require live Stripe

Checks that cannot be automated are printed as MANUAL. They are not optional —
they are the ones a human has to sign off, most importantly the real-card
purchase, because the money path has never been exercised.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS, FAIL, WARN, MANUAL = "PASS", "FAIL", "WARN", "MANUAL"

_COLOURS = {PASS: "\033[32m", FAIL: "\033[31m", WARN: "\033[33m", MANUAL: "\033[36m"}
_RESET = "\033[0m"

results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    results.append((status, name, detail))


# ── Stripe ────────────────────────────────────────────────────────────────────

def check_stripe(require_live: bool) -> None:
    from app.api.stripe_checkout import MODE_MAP, _get_price
    from app.billing import stripe_mode as sm

    mode = sm.secret_key_mode()

    if require_live:
        if mode == sm.LIVE:
            record(PASS, "Stripe mode", "live key configured")
        else:
            record(
                FAIL,
                "Stripe mode",
                f"--live requested but the secret key is {mode!r}. No real card "
                f"can be charged; only Stripe test cards will succeed.",
            )
    else:
        record(
            WARN if mode != sm.LIVE else PASS,
            "Stripe mode",
            f"{mode} — no real money moves in test mode. See GO_LIVE.md."
            if mode != sm.LIVE
            else "live",
        )

    for problem in sm.check_consistency():
        record(FAIL, "Stripe config", problem)

    # Every sellable SKU needs a price, or its buy button dead-ends. Checked
    # here rather than trusted, because a missing price surfaces only as a 400
    # at the moment a customer clicks Buy.
    missing = sorted(s for s in MODE_MAP if not _get_price(s))
    if missing:
        record(
            FAIL,
            "Stripe prices",
            f"{len(missing)} sellable SKU(s) have no price configured: "
            + ", ".join(missing[:8])
            + (" …" if len(missing) > 8 else ""),
        )
    else:
        record(PASS, "Stripe prices", f"all {len(MODE_MAP)} SKUs resolve to a price")


# ── Database ──────────────────────────────────────────────────────────────────

def check_migrations() -> None:
    """A deploy without its migration takes down every path touching the table.

    This is not hypothetical: it happened on 2026-08-08 and `/api/health`
    reported healthy throughout, because a push to `main` does NOT migrate —
    `RUN_MIGRATIONS_ON_BOOT` is unset in ECS and migrations run only via
    workflow_dispatch.
    """
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        from sqlalchemy import create_engine, text

        root = Path(__file__).resolve().parent.parent
        head = ScriptDirectory.from_config(Config(str(root / "alembic.ini"))).get_current_head()

        url = os.environ.get("DATABASE_URL")
        if not url:
            record(MANUAL, "Migrations", "DATABASE_URL unset — cannot compare to head")
            return

        with create_engine(url).connect() as conn:
            current = conn.execute(text("select version_num from alembic_version")).scalar()

        if current == head:
            record(PASS, "Migrations", f"database at head ({head})")
        else:
            record(FAIL, "Migrations", f"database at {current}, code expects {head}")
    except Exception as exc:
        record(MANUAL, "Migrations", f"could not check: {exc}")


# ── Truthfulness ──────────────────────────────────────────────────────────────

def check_scout_compliance() -> None:
    """Outreach without a postal address breaches the Spam Control Act."""
    from app.core.config import settings
    from app.workers.celery_app import celery_app

    enabled = "scout-send-approved-daily" in (celery_app.conf.beat_schedule or {})
    postal = (settings.COMPANY_POSTAL_ADDRESS or "").strip()

    if not enabled:
        record(PASS, "SCOUT outreach", "send task disabled — nothing can go out")
    elif not postal:
        record(
            FAIL,
            "SCOUT outreach",
            "send task ENABLED but COMPANY_POSTAL_ADDRESS is unset — "
            "Spam Control Act (Cap. 311A) Second Schedule para 2(b)",
        )
    else:
        record(PASS, "SCOUT outreach", "enabled with a postal address configured")


def check_retention_promises() -> None:
    """The privacy policy is a promise; unimplemented ones become breaches.

    Retaining LESS than promised harms nobody. Retaining longer — which is what
    an unimplemented purge means — is the direction that breaches PDPA/GDPR.
    Nothing is old enough to be in breach yet, which is exactly why this needs
    to be visible before the clock starts on real customer data.
    """
    from app.services.retention import RETENTION_CATEGORIES

    promised_but_unbuilt = {
        "Compliance reports and certificates: 5 years from generation",
        "Account data: duration of account plus 7 years after closure",
        "Marketing consent records: until withdrawal plus 3 years",
    }
    record(
        MANUAL,
        "Retention schedule",
        f"{len(RETENTION_CATEGORIES)} category purges implemented; these published "
        f"commitments have no mechanism: " + "; ".join(sorted(promised_but_unbuilt)),
    )


def check_frontend_contract() -> None:
    """A frontend call to a route that does not exist is a dead button."""
    import subprocess

    root = Path(__file__).resolve().parent.parent
    try:
        proc = subprocess.run(
            [sys.executable, str(root / "scripts" / "check_api_contract.py")],
            capture_output=True, text=True, timeout=120,
        )
    except Exception as exc:
        record(MANUAL, "FE↔BE contract", f"could not run: {exc}")
        return

    if proc.returncode == 0:
        last = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        record(PASS, "FE↔BE contract", last[-1] if last else "clean")
    else:
        record(FAIL, "FE↔BE contract", proc.stdout.strip().splitlines()[-1] if proc.stdout else "unmatched paths")


# ── Things only a human can confirm ───────────────────────────────────────────

MANUAL_CHECKS = [
    ("Real card purchase", "One low-value SKU end to end: card charged → webhook "
     "received → entitlement granted → deliverable received. The money path has "
     "NEVER been exercised; the test suite runs entirely against mocks."),
    ("Live webhook endpoint", "Created in Stripe live mode and its signing secret "
     "copied to booppa/app-secrets. If forgotten: payment succeeds, signature "
     "verification fails, customer charged and nothing delivered."),
    ("Secret rotation", "COOKIE_SIGNING_SECRET has been readable in plaintext in "
     "Amplify. It gates the vendor_plan paywall — rotate before real customers."),
    ("Government portal", "Self-serve .gov.sg verification does not exist; access "
     "requests go to the support queue. Confirm someone is watching that queue."),
    ("Refund policy", "A dispute revokes report access; a refund we issue does not. "
     "Confirm this matches what the terms page tells customers."),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true",
                        help="require live Stripe credentials (fail on test mode)")
    args = parser.parse_args()

    for check in (
        lambda: check_stripe(args.live),
        check_migrations,
        check_scout_compliance,
        check_retention_promises,
        check_frontend_contract,
    ):
        try:
            check()
        except Exception as exc:
            record(FAIL, check.__name__ if hasattr(check, "__name__") else "check",
                   f"raised {type(exc).__name__}: {exc}")

    for name, detail in MANUAL_CHECKS:
        record(MANUAL, name, detail)

    width = max(len(n) for _, n, _ in results) + 2
    print()
    for status, name, detail in results:
        colour = _COLOURS.get(status, "")
        print(f"  {colour}{status:<6}{_RESET} {name:<{width}} {detail}")

    failures = [r for r in results if r[0] == FAIL]
    manual = [r for r in results if r[0] == MANUAL]
    print()
    if failures:
        print(f"  {len(failures)} blocking issue(s). Do not launch.")
        return 1
    print(f"  Mechanical checks pass. {len(manual)} item(s) still need a human.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
