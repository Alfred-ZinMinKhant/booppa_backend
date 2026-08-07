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
# `prospect` is a ScoutProspect: a cold-outreach candidate we scraped and scored,
# with its own scraped `uen`. It is never an account, and nothing reads it as one —
# the SCOUT pipeline checks a prospect's UEN against User rows precisely to EXCLUDE
# existing customers, rather than to write to them.
NON_USER_RECEIVERS = re.compile(
    r"\b(row|intake|profile|dv|vendor|record|pack|snapshot|entry|obj|payload"
    r"|discovered|candidate|prospect|tender|result)\.\w+\s*=(?!=)"
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


# ---------------------------------------------------------------------------
# Renaming an account is the OTHER half of the same invariant.
# ---------------------------------------------------------------------------
#
# The test above guards writes to `User.uen` / `User.legal_name`. It says nothing
# about `User.company`, which is not itself an identity value — and that is exactly
# how `PATCH /vendor/profile` shipped a rename that left `uen` and `legal_name_hint`
# behind from the previous company.
#
# That residue is not dormant. `_cache_trusted_for_hint` treats a `company_hint`
# equal to the account's own `company` as a self-render (`own_name_implies_self`),
# so after a rename the NEW name vouches for the OLD company's UEN, and the registry
# confirms it as a real entity. The read-side gate cannot catch this; it has to be
# enforced where the rename happens.
#
# The rule: a module that can rename an account must also know how to invalidate
# what the old name vouched for.

# Writes to the two fields whose change invalidates a cached identity. Both matter:
# `website_hint` is checked independently of the company string, because trading
# names collide and several deliverables render the domain as the assessed subject.
RENAME_RE = re.compile(r"\buser\.(company|website)\s*=(?!=)")

# Sites that assign these fields but cannot rename, so they have nothing to clear.
# Each is fill-when-empty (`if not user.website`, `if body.company and not
# existing.company`) or a test/demo harness operating on a throwaway account.
#
# Adding a file here is a claim that it can never overwrite a NON-EMPTY company or
# website. If that stops being true, it belongs in the clearing set instead.
RENAME_EXEMPT = {
    "api/stripe_checkout.py",   # fill-only, and gated on `is_self`
    "api/vendor_features.py",   # fill-only, subsidiary invite
    "api/admin.py",             # test-checkout harness
    "services/pro_suite_demo_harness.py",
    "services/csp_demo_harness.py",
}


def test_account_rename_paths_invalidate_the_cached_identity():
    offenders: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        rel = path.relative_to(APP_ROOT).as_posix()
        if rel in RENAME_EXEMPT:
            continue
        source = path.read_text()
        renames = [
            (n, line.strip())
            for n, line in enumerate(source.splitlines(), start=1)
            if RENAME_RE.search(line) and not line.strip().startswith("#")
        ]
        if not renames:
            continue
        # Presence of the call is a coarse check — it cannot prove the clear happens
        # on the right branch. Its job is to stop a NEW rename path from being added
        # with no awareness of the invariant at all, which is how this shipped twice.
        if "clear_cached_identity" in source:
            continue
        for lineno, line in renames:
            offenders.append(f"app/{rel}:{lineno}: {line}")

    assert not offenders, (
        "A module writes `user.company`/`user.website` but never calls "
        "clear_cached_identity. A rename invalidates the account's cached `uen` and "
        "`legal_name` — they were confirmed for the previous company and belong to a "
        "different legal person now. Leaving them behind does not merely go stale: "
        "the account's new name vouches for the old company's UEN via "
        "`own_name_implies_self`, and ACRA returns a real, correct record for the "
        "wrong entity.\n\n"
        "Clear then re-resolve (see app/api/auth.py and app/api/vendor_status.py). "
        "If this site can only fill an EMPTY field and never overwrite one, add it to "
        "RENAME_EXEMPT in this test with a note saying which guard makes that true."
        "\n\n" + "\n".join(offenders)
    )


def test_the_rename_exemptions_still_exist():
    """An exemption for a deleted or renamed file silently widens the guard."""
    missing = [rel for rel in RENAME_EXEMPT if not (APP_ROOT / rel).exists()]
    assert not missing, f"RENAME_EXEMPT names files that no longer exist: {missing}"
