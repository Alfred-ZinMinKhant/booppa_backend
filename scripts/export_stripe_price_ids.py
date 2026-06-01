"""
Export Stripe price IDs from prices.csv as KEY=value lines.

Used by CI (`.github/workflows/test.yml`) to avoid keeping a separate
STRIPE_PRICE_IDS_JSON secret in sync with the live Stripe catalogue. The CSV
is the human-editable source of truth — committing a row updates CI on the
next push, and dev/prod can run the same script locally to populate `.env`.

When a Product Name appears multiple times (e.g. after re-pricing a SKU),
the row with the most recent `Created (UTC)` wins; older entries refer to
archived/inactive Stripe products and would 500 at checkout.

Usage:
    # In CI: append to GITHUB_ENV
    python scripts/export_stripe_price_ids.py >> "$GITHUB_ENV"

    # Locally: write to a .env-style file
    python scripts/export_stripe_price_ids.py > .env.stripe

    # Custom CSV path
    python scripts/export_stripe_price_ids.py path/to/prices.csv
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime
from pathlib import Path


def _parse_created(value: str) -> datetime:
    # Stripe export format: "2026-06-01 04:29"
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        return datetime.min


def export_price_ids(csv_path: Path) -> dict[str, str]:
    """Return {STRIPE_KEY: price_id} keeping the newest row per key."""
    latest: dict[str, tuple[datetime, str]] = {}
    with csv_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("Product Name") or "").strip()
            price_id = (row.get("Price ID") or "").strip()
            if not name.startswith("STRIPE_") or not price_id:
                continue
            created = _parse_created(row.get("Created (UTC)") or "")
            prev = latest.get(name)
            if prev is None or created > prev[0]:
                latest[name] = (created, price_id)
    return {name: pid for name, (_, pid) in latest.items()}


def main(argv: list[str]) -> int:
    csv_path = Path(argv[1] if len(argv) > 1 else "prices.csv")
    if not csv_path.exists():
        print(f"prices.csv not found at {csv_path}", file=sys.stderr)
        return 1
    mapping = export_price_ids(csv_path)
    if not mapping:
        print("no STRIPE_* rows found in prices.csv", file=sys.stderr)
        return 1
    for key in sorted(mapping):
        print(f"{key}={mapping[key]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
