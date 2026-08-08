"""Every `productType` the frontend can send must exist in MODE_MAP.

A `productType` that MODE_MAP does not know is not a loud failure. Checkout
resolves no price and the buyer sees a generic error, or — worse, if a session
is created some other way — the webhook logs "no handler matched" and returns
200, so Stripe is satisfied, the money is taken, and nothing is ever delivered.

`quick_fix` shipped on two blog CTAs in exactly that state
(AUDIT_2026-08-08.md P1-1) and nothing in the build noticed. This is the check
that would have.

Skips when the sibling frontend repo is absent, the same convention
`scripts/check_api_contract.py` uses — CI checks it out explicitly.
"""
import re
from pathlib import Path

import pytest

from app.api.stripe_checkout import MODE_MAP

_REPO = Path(__file__).resolve().parents[2]
# Sibling checkout locally; CI clones it to `frontend/` inside the workspace.
FRONTEND = next(
    (p for p in (_REPO.parent / "booppa-nextjs", _REPO / "frontend") if p.is_dir()),
    _REPO / "frontend",
)
SKIP_DIRS = {"node_modules", ".next", ".git", "dist", "build", "coverage", "e2e"}

# `productType: "x"`, `product_type: 'x'`, and the JSON-body spellings of both.
#
# The trailing `(?!\s*\|)` excludes TypeScript union *types*
# (`rfp_product_type: 'rfp_express' | 'rfp_complete' | string;`). Those declare
# what a value may be, not a value being sent, and they legitimately name
# components of bundles — `rfp_express` is a kit produced inside a bundle, never
# a SKU sold on its own, so it is correctly absent from MODE_MAP.
PRODUCT_TYPE = re.compile(
    r"""["']?product_?[Tt]ype["']?\s*:\s*["'`]([a-z0-9_]+)["'`](?!\s*\|)"""
)
# Whole-line comments are prose. A comment naming a retired SKU to explain why
# it was removed must not read as a live call site.
COMMENT_LINE = re.compile(r"\s*(//|/\*|\*)")


def _frontend_product_types() -> dict[str, list[str]]:
    """{product_type: ["path:line", …]} across the frontend source."""
    found: dict[str, list[str]] = {}
    for path in FRONTEND.rglob("*.ts*"):
        if path.suffix not in (".ts", ".tsx"):
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(lines, 1):
            if COMMENT_LINE.match(line):
                continue
            for m in PRODUCT_TYPE.finditer(line):
                rel = path.relative_to(FRONTEND)
                found.setdefault(m.group(1), []).append(f"{rel}:{lineno}")
    return found


@pytest.mark.skipif(not FRONTEND.is_dir(), reason="sibling frontend repo not present")
def test_every_frontend_product_type_is_sellable():
    found = _frontend_product_types()
    assert found, "found no productType literals at all — the regex has gone stale"

    unknown = {k: v for k, v in found.items() if k not in MODE_MAP}
    assert not unknown, "\n".join(
        ["productType(s) the backend cannot sell:"]
        + [f"  {k!r} at {', '.join(sites)}" for k, sites in sorted(unknown.items())]
    )


def test_advertised_skus_have_a_price_var_wired_into_deploys():
    """A SKU the frontend sells must have its price var wired into `ci.yml`.

    `_get_price` reads `STRIPE_<SKU>` from the environment at call time, so a
    SKU added to MODE_MAP without a matching entry in the task definition
    resolves to None and the Buy button dead-ends at a 400 — visible only to a
    customer clicking it. Checking the workflow file instead of `os.environ`
    makes this env-independent, so it fails in CI rather than at launch.

    Scoped to SKUs the frontend actually advertises: MODE_MAP legitimately
    carries unlaunched products (`trust_passport_api`, `trust_passport_monitor`)
    that no page sells yet.
    """
    import re
    from pathlib import Path

    from app.api.stripe_checkout import _PRICE_FALLBACKS

    ci = (Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml").read_text()
    wired = set(re.findall(r'"name":\s*"(STRIPE_[A-Z0-9_]+)"', ci))

    advertised = [s for s in _frontend_product_types() if s in MODE_MAP]
    missing = []
    for sku in sorted(advertised):
        keys = {f"STRIPE_{sku.upper()}"}
        if sku in _PRICE_FALLBACKS:
            keys.add(f"STRIPE_{_PRICE_FALLBACKS[sku].upper()}")
        if not (keys & wired):
            missing.append(sku)

    assert not missing, (
        "advertised on the frontend but no STRIPE_* price var in ci.yml, so the "
        "Buy button dead-ends in production: " + ", ".join(missing)
    )


@pytest.mark.skipif(not FRONTEND.is_dir(), reason="sibling frontend repo not present")
def test_every_advertised_product_type_has_a_configured_price():
    """A SKU in MODE_MAP whose price env var is unset cannot complete checkout."""
    import os

    from app.api.stripe_checkout import _get_price

    missing = []
    for product_type in sorted(_frontend_product_types()):
        if product_type not in MODE_MAP:
            continue  # covered by the test above
        try:
            price = _get_price(product_type)
        except Exception:
            price = None
        if not price:
            missing.append(product_type)

    if missing and not os.environ.get("CI"):
        pytest.skip(f"price env vars unset locally for: {', '.join(missing)}")
    assert not missing, (
        "advertised on the frontend but no Stripe price configured: "
        + ", ".join(missing)
    )
