"""Frontend and backend prices must not drift apart.

Both files call themselves the single source of truth — `app/services/pricing.py`
says "Both backend and frontend should reference this", and
`booppa-nextjs/lib/pricing.ts` says "Prices here must match
app/services/pricing.py". Neither imports the other, so the claim is enforced by
nothing but attention (AUDIT_2026-08-08.md P2-6).

They agree today. The failure mode is a price changed on one side only: the page
advertises one number, Stripe charges another, and the buyer finds out on their
card statement. Merging the two files properly means generating the TS from the
Python at build time; until someone does that, this test is the guard.
"""
import re
from pathlib import Path

import pytest

from app.services.pricing import PRODUCTS

_REPO = Path(__file__).resolve().parents[2]
FRONTEND = next(
    (p for p in (_REPO.parent / "booppa-nextjs", _REPO / "frontend") if p.is_dir()),
    _REPO / "frontend",
)
PRICING_TS = FRONTEND / "lib" / "pricing.ts"

# `vendor_proof: { … price: 149 …` — the key, then the first `price:` after it.
ENTRY = re.compile(r"(\w+)\s*:\s*\{[^}]*?\bprice\s*:\s*(\d+)", re.S)


def _frontend_prices() -> dict[str, int]:
    return {k: int(v) for k, v in ENTRY.findall(PRICING_TS.read_text(encoding="utf-8"))}


@pytest.mark.skipif(not PRICING_TS.is_file(), reason="sibling frontend repo not present")
def test_shared_skus_have_identical_prices():
    fe = _frontend_prices()
    assert fe, "parsed no prices from lib/pricing.ts — the regex has gone stale"

    shared = sorted(set(fe) & set(PRODUCTS))
    assert len(shared) >= 20, (
        f"only matched {len(shared)} SKUs across the two files; expected most of "
        f"{len(PRODUCTS)}. One side was probably restructured and this test is "
        f"now comparing almost nothing."
    )

    drift = {
        sku: (PRODUCTS[sku]["price_sgd"], fe[sku])
        for sku in shared
        if PRODUCTS[sku].get("price_sgd") != fe[sku]
    }
    assert not drift, "\n".join(
        ["price drift between backend and frontend (backend, frontend):"]
        + [f"  {sku}: SGD {b} vs SGD {f}" for sku, (b, f) in sorted(drift.items())]
    )


@pytest.mark.skipif(not PRICING_TS.is_file(), reason="sibling frontend repo not present")
def test_price_cents_matches_price_sgd():
    """`price_cents` is what reaches Stripe; `price_sgd` is what the page shows."""
    wrong = {
        sku: (p["price_sgd"], p["price_cents"])
        for sku, p in PRODUCTS.items()
        if "price_cents" in p
        and "price_sgd" in p
        and p["price_cents"] != p["price_sgd"] * 100
    }
    assert not wrong, f"price_cents != price_sgd * 100 for: {wrong}"
