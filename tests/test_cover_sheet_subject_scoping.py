"""Cover Sheets are scoped to ONE subject — a reused account must not leak.

Incident: a buyer account used for two vendors in sequence (Netpoleon, then
Adnovum) produced a Netpoleon-branded, blockchain-anchored Compliance Cover
Sheet carrying Adnovum's PDPA score (53 instead of 58). Every input query in the
pipeline selected "latest row by owner_id" with no notion of *which purchase*,
so the newest scan won a created_at race it should never have been in.

The subject is the website domain — the same rule `evidence_enricher`
already applies to cached identity ("a different site is a different subject").

Two properties are under test:
  1. Scoping — no artifact belonging to another subject may back a sheet.
  2. Fallbacks — scoping must not turn into a delivery outage for the legacy /
     single-vendor accounts that have no domain recorded at all.
"""
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

import app.services.fulfillment.helpers as wh
import app.workers.tasks as tasks_mod
from app.core.models import EvidencePack, Report, User
from app.services.fulfillment.helpers import (
    norm_site,
    resolve_cover_sheet_subject,
    scope_rows_to_subject,
)


# ── scope_rows_to_subject: the safety rules ──────────────────────────────────

class _Row:
    def __init__(self, company_website=None, tag=None):
        self.company_website = company_website
        self.tag = tag

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<Row {self.tag or self.company_website}>"


def test_matching_rows_win():
    net = _Row("https://netpoleon.com", "net")
    adn = _Row("https://adnovum.com", "adn")
    assert scope_rows_to_subject([adn, net], "netpoleon.com") == [net]


def test_domain_forms_normalise():
    """`https://WWW.Netpoleon.com/sg` and `netpoleon.com` are one subject — a
    raw string compare would have silently matched nothing and deferred forever."""
    row = _Row("https://WWW.Netpoleon.com/sg")
    assert scope_rows_to_subject([row], "netpoleon.com") == [row]


def test_no_row_carries_a_domain_returns_everything():
    """Legacy rows predating `Report.company_website`: no scoping information
    exists AND there is nothing to leak between, so pre-fix behaviour stands.
    This is the case that keeps existing single-vendor buyers getting sheets."""
    rows = [_Row(None), _Row("")]
    assert scope_rows_to_subject(rows, "netpoleon.com") == rows


def test_rows_exist_for_other_subjects_only_returns_empty():
    """The incident shape. Rows are attributable, none is ours -> caller must
    defer. Returning the foreign row here is what anchored the wrong sheet."""
    assert scope_rows_to_subject([_Row("https://adnovum.com")], "netpoleon.com") == []


def test_unresolved_subject_is_safe_while_account_is_unambiguous():
    rows = [_Row("https://acme.test")]
    assert scope_rows_to_subject(rows, "") == rows


def test_unresolved_subject_refuses_once_account_is_ambiguous():
    rows = [_Row("https://acme.test"), _Row("https://other.test")]
    assert scope_rows_to_subject(rows, "") == []


def test_include_unknown_keeps_domainless_rows():
    """ROPA / signed-sheet rows carry no website. On an index list they must be
    kept — dropping them strips real deliverables off DOCUMENTS ANCHORED."""
    net = _Row("https://netpoleon.com", "net")
    ropa = _Row(None, "ropa")
    adn = _Row("https://adnovum.com", "adn")
    got = scope_rows_to_subject([net, ropa, adn], "netpoleon.com", include_unknown=True)
    assert got == [net, ropa]
    # ...but strict mode (headline numbers) still refuses the unattributable row.
    assert scope_rows_to_subject([net, ropa, adn], "netpoleon.com") == [net]


def test_custom_getter_reads_pack_intake_domain():
    pack = EvidencePack(
        id=uuid.uuid4(), pack_id="BCEP-X", user_id=uuid.uuid4(),
        intake={"domain": "netpoleon.com"},
    )
    got = scope_rows_to_subject(
        [pack], "netpoleon.com",
        getter=lambda p: (p.intake or {}).get("domain"),
    )
    assert got == [pack]


# ── _maybe_fire_cover_sheet: end-to-end scoping ──────────────────────────────

def _install(test_db, monkeypatch):
    monkeypatch.setattr(wh, "SessionLocal", sessionmaker(bind=test_db.get_bind()))
    fired = []
    monkeypatch.setattr(
        tasks_mod.fulfill_cover_sheet_task,
        "apply_async",
        lambda *a, **k: fired.append(k) or None,
    )
    return fired


def _user(test_db, **kw):
    user = User(
        email=f"scope-{uuid.uuid4().hex[:8]}@test.io",
        hashed_password="x",
        pending_cover_sheet=True,
        **kw,
    )
    test_db.add(user)
    test_db.commit()
    test_db.refresh(user)
    return user


def _scan(test_db, user, website, score, *, age_days=0):
    test_db.add(Report(
        id=uuid.uuid4(), owner_id=user.id, framework="pdpa_quick_scan",
        company_name=website, company_website=website, status="completed",
        assessment_data={"overall_risk_score": score},
        created_at=datetime.utcnow() - timedelta(days=age_days),
    ))
    test_db.commit()


def _kit(test_db, user, website, *, age_days=0):
    test_db.add(Report(
        id=uuid.uuid4(), owner_id=user.id, framework="rfp_complete",
        company_name=website, company_website=website, status="completed",
        assessment_data={"product_type": "rfp_complete"},
        created_at=datetime.utcnow() - timedelta(days=age_days),
    ))
    test_db.commit()


def _pack(test_db, user, domain, org, *, status="ready", age_days=0):
    row = EvidencePack(
        id=uuid.uuid4(), pack_id=f"BCEP-{uuid.uuid4().hex[:8]}",
        user_id=user.id, status=status, organisation=org,
        intake={"org_name": org, "domain": domain},
        created_at=datetime.utcnow() - timedelta(days=age_days),
    )
    test_db.add(row)
    test_db.commit()
    return row


def test_newer_subject_does_not_steal_the_older_purchases_sheet(test_db, monkeypatch):
    """The incident, reproduced. Netpoleon's inputs are complete; Adnovum's pack
    is newer but has no scan yet. The sheet that fires must be Netpoleon's, and
    it must be told so explicitly via target_domain."""
    fired = _install(test_db, monkeypatch)
    user = _user(test_db)
    _scan(test_db, user, "https://netpoleon.com", 58, age_days=3)
    _kit(test_db, user, "https://netpoleon.com", age_days=3)
    _pack(test_db, user, "netpoleon.com", "Netpoleon", age_days=3)
    # Newer purchase for a different vendor — scan not back yet.
    _pack(test_db, user, "adnovum.com", "Adnovum", status="intake_pending")

    wh._maybe_fire_cover_sheet(user.email)

    domains = [k["kwargs"]["target_domain"] for k in fired]
    assert domains == ["netpoleon.com"], (
        "the newer Adnovum pack must not claim the sheet, and Netpoleon's must fire"
    )
    # Adnovum is still owed a sheet -> the flag must survive this pass.
    test_db.expire_all()
    assert test_db.get(User, user.id).pending_cover_sheet is True


def test_foreign_scan_never_backs_a_sheet(test_db, monkeypatch):
    """Only Adnovum has a scan; the pending sheet is Netpoleon's. Firing with
    Adnovum's 53 is the bug — deferring is the correct behaviour."""
    fired = _install(test_db, monkeypatch)
    user = _user(test_db, compliance_evidence_rfp_ready=True)
    _pack(test_db, user, "netpoleon.com", "Netpoleon")
    _scan(test_db, user, "https://adnovum.com", 53)

    wh._maybe_fire_cover_sheet(user.email)

    assert fired == []
    test_db.expire_all()
    assert test_db.get(User, user.id).pending_cover_sheet is True


def test_only_one_sheet_fires_per_invocation(test_db, monkeypatch):
    """Two ready subjects must NOT both fire in one pass.

    Firing them together sent four cover-sheet emails in a single minute: each
    subject has a different anchored-document fingerprint, so the task's 24h
    content-aware email guard gives each its own dedupe key and suppresses
    nothing (and test_simulation runs skip that guard outright). One per pass;
    the flag stays set so the hourly sweep takes the next.
    """
    fired = _install(test_db, monkeypatch)
    user = _user(test_db)
    for site, org in (("netpoleon.com", "Netpoleon"), ("adnovum.com", "Adnovum")):
        _scan(test_db, user, f"https://{site}", 58)
        _kit(test_db, user, f"https://{site}")
        _pack(test_db, user, site, org)

    wh._maybe_fire_cover_sheet(user.email)

    assert len(fired) == 1, "one sheet per invocation — not a burst of emails"
    test_db.expire_all()
    assert test_db.get(User, user.id).pending_cover_sheet is True, (
        "the second subject is still owed a sheet, so the flag must survive"
    )


def test_second_subject_fires_on_the_next_sweep(test_db, monkeypatch):
    """RC-2 still holds: each subject eventually gets its own sheet, one pass at
    a time, once the first has recorded its delivery."""
    fired = _install(test_db, monkeypatch)
    user = _user(test_db)
    for site, org in (("netpoleon.com", "Netpoleon"), ("adnovum.com", "Adnovum")):
        _scan(test_db, user, f"https://{site}", 58)
        _kit(test_db, user, f"https://{site}")
        _pack(test_db, user, site, org)

    wh._maybe_fire_cover_sheet(user.email)
    first = fired[0]["kwargs"]["target_domain"]
    # The task records delivery by writing a scoped compliance_evidence_pack row.
    test_db.add(Report(
        id=uuid.uuid4(), owner_id=user.id, framework="compliance_evidence_pack",
        company_name=first, company_website=f"https://{first}", status="completed",
        assessment_data={"bundle_type": "compliance_evidence_pack"},
    ))
    test_db.commit()

    wh._maybe_fire_cover_sheet(user.email)

    assert [k["kwargs"]["target_domain"] for k in fired] == [
        first, "adnovum.com" if first == "netpoleon.com" else "netpoleon.com",
    ]
    test_db.expire_all()
    assert test_db.get(User, user.id).pending_cover_sheet is False


def test_legacy_sheet_without_a_domain_is_not_resent(test_db, monkeypatch):
    """The actual duplicate-email cause: sheets delivered before subject scoping
    carry NULL company_website. Matching on domain alone made every one of them
    look undelivered and re-sent the lot. Fall back to the company name."""
    fired = _install(test_db, monkeypatch)
    user = _user(test_db)
    _scan(test_db, user, "https://netpoleon.com", 58)
    _kit(test_db, user, "https://netpoleon.com")
    _pack(test_db, user, "netpoleon.com", "Netpoleon")
    test_db.add(Report(
        id=uuid.uuid4(), owner_id=user.id, framework="compliance_evidence_pack",
        company_name="Netpoleon", company_website=None, status="completed",
        assessment_data={"bundle_type": "compliance_evidence_pack"},
    ))
    test_db.commit()

    wh._maybe_fire_cover_sheet(user.email)

    assert fired == []


def test_subject_with_a_delivered_sheet_is_not_refired(test_db, monkeypatch):
    """A `compliance_evidence_pack` Report records which subject was delivered."""
    fired = _install(test_db, monkeypatch)
    user = _user(test_db)
    _scan(test_db, user, "https://netpoleon.com", 58)
    _kit(test_db, user, "https://netpoleon.com")
    _pack(test_db, user, "netpoleon.com", "Netpoleon")
    test_db.add(Report(
        id=uuid.uuid4(), owner_id=user.id, framework="compliance_evidence_pack",
        company_name="Netpoleon", company_website="https://netpoleon.com",
        status="completed", assessment_data={"bundle_type": "compliance_evidence_pack"},
    ))
    test_db.commit()

    wh._maybe_fire_cover_sheet(user.email)

    assert fired == []


def test_single_vendor_account_without_domains_still_fires(test_db, monkeypatch):
    """The regression guard on the fix itself: a legacy buyer whose rows carry
    no `company_website` must still get their cover sheet, not be starved by a
    filter that can never match."""
    fired = _install(test_db, monkeypatch)
    user = _user(test_db, compliance_evidence_rfp_ready=True)
    test_db.add(Report(
        id=uuid.uuid4(), owner_id=user.id, framework="pdpa_quick_scan",
        company_name="Legacy Pte Ltd", company_website=None, status="completed",
        assessment_data={"overall_risk_score": 61},
    ))
    test_db.commit()
    _pack(test_db, user, "", "Legacy Pte Ltd")

    wh._maybe_fire_cover_sheet(user.email)

    assert len(fired) == 1
    test_db.expire_all()
    assert test_db.get(User, user.id).pending_cover_sheet is False


# ── subject resolution ───────────────────────────────────────────────────────

def test_subject_comes_from_the_packs_own_intake(test_db):
    user = _user(test_db)
    pack = _pack(test_db, user, "https://www.netpoleon.com/sg", "Netpoleon")
    assert resolve_cover_sheet_subject(test_db, user, pack) == "netpoleon.com"


def test_subject_falls_back_to_the_rfp_kits_website(test_db):
    user = _user(test_db)
    _kit(test_db, user, "https://netpoleon.com")
    pack = _pack(test_db, user, "", "Netpoleon")
    assert resolve_cover_sheet_subject(test_db, user, pack) == "netpoleon.com"


def test_subject_never_falls_back_to_the_account_website(test_db):
    """`User.website` is the account holder's site. On a reseller account that
    is not the subject, and a confidently wrong subject is worse than none."""
    user = _user(test_db, website="https://reseller.example")
    assert resolve_cover_sheet_subject(test_db, user, None) == ""


@pytest.mark.parametrize("raw,expected", [
    ("https://WWW.Acme.com/about", "acme.com"),
    ("acme.com.", "acme.com"),
    (None, ""),
    ("", ""),
])
def test_norm_site(raw, expected):
    assert norm_site(raw) == expected
