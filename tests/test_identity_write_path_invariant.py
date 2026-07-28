"""Structural guard: `User.uen` / `User.legal_name` may only be written in one place.

The same identity-leak bug has now been found three times in three different files
(evidence_enricher, rfp_express_builder, fulfillment/single_products). Each was a
direct assignment to the account's cached identity that skipped the provenance/trust
check. Patching them one at a time doesn't converge, so this test makes a fourth
variant fail CI instead of shipping.

Route every write through
`app.services.evidence_enricher.record_verified_identity(...)`, which performs the
`_cache_trusted_for_hint` check and records `legal_name_hint` / `website_hint`
provenance alongside the value.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

# The single sanctioned write path. Everything else must call into it.
ALLOWED = {"services/evidence_enricher.py"}

# `<something>.uen = ...` / `.legal_name = ...` / `.legal_name_hint = ...` /
# `.website_hint = ...`, but not `==`, and not a keyword argument (`uen=`), which
# has no leading dot.
# `verified_hint` is the same provenance column under the two subject carriers
# (`DiscoveredVendor`, `ManagedEntity`). It is what makes a cached UEN trustworthy,
# so writing it by hand — or writing a `uen` without it — reintroduces exactly the
# unprovenanced-cache signature the trust check exists to reject.
ASSIGN_RE = re.compile(
    r"\.(uen|legal_name|legal_name_hint|website_hint|verified_hint)\s*=(?!=)"
)

# Assignments on objects that are demonstrably NOT a User row. These carry their own
# identity fields and are not the account-level cache that leaks across purchases.
NON_USER_RECEIVERS = re.compile(
    r"\b(row|intake|profile|dv|vendor|record|pack|snapshot|entry|obj|payload"
    r"|discovered|candidate|tender|result)\.\w+\s*=(?!=)"
)


def _offending_lines(path: Path) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if line.startswith("#"):
            continue
        # The model definition itself (`uen = Column(...)`) has no leading dot, so it
        # never matches ASSIGN_RE — nothing to exclude there.
        if not ASSIGN_RE.search(line):
            continue
        if NON_USER_RECEIVERS.search(line):
            continue
        hits.append((lineno, line))
    return hits


def test_user_identity_is_written_only_by_record_verified_identity():
    offenders: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        rel = path.relative_to(APP_ROOT).as_posix()
        if rel in ALLOWED:
            continue
        for lineno, line in _offending_lines(path):
            offenders.append(f"app/{rel}:{lineno}: {line}")

    assert not offenders, (
        "Direct write to a User identity column outside evidence_enricher.py.\n"
        "Call evidence_enricher.record_verified_identity(user, db, uen=..., "
        "legal_name=..., company_hint=..., website_hint=...) instead — it applies the "
        "trust check and records provenance. If the receiver here is NOT a User row, "
        "add it to NON_USER_RECEIVERS in this test.\n\n" + "\n".join(offenders)
    )


def test_the_sanctioned_write_path_exists():
    """Guards against the allowlist silently covering a renamed/deleted function."""
    from app.services import evidence_enricher

    assert callable(evidence_enricher.record_verified_identity)
    assert callable(evidence_enricher.trusted_cached_uen)
    assert callable(evidence_enricher._cache_trusted_for_hint)
