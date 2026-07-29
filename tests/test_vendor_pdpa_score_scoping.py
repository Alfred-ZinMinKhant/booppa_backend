"""Regression: a vendor's own deliverables must not print a client's PDPA score.

``latest_pdpa_score(db, user_id)`` with no ``domain`` returns the account-wide
latest score. Its own docstring flags this as the account-cache contamination
vector behind the SPQR UEN leak, but the vendor-account surfaces — status
snapshot, monthly digest, Vendor Pro report, proof badge, and the tender
win-probability multiplier — all called it unscoped.

An agency or consultancy on Vendor Active that scans a client's website from the
same login would get that client's number rendered under its own company name.

``vendor_pdpa_score(db, user)`` scopes by ``User.website``. A report whose
recorded website *contradicts* it is a different subject and is rejected; one
with no recorded website is unattributed, not contradictory, and is still
accepted — ``ReportRequest.website`` is optional, so rejecting those would
strand vendors whose only scan omitted it.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.services.pdpa_findings import latest_pdpa_score, vendor_pdpa_score

OWN_SCORE = 58     # scan of the vendor's own site
CLIENT_SCORE = 91  # scan of someone else's site, run from the same account


def _vendor(db, email, website):
    from app.core.models import User

    u = User(email=email, hashed_password="x", role="VENDOR",
             plan="vendor_active", company="Agency Pte Ltd", website=website)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _scan(db, user, website, score, age_days):
    from app.core.models import Report

    db.add(Report(
        owner_id=user.id,
        framework="pdpa_quick_scan",
        company_name="Subject",
        company_website=website,
        assessment_data={"compliance_score": score},
        status="completed",
        created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
        completed_at=datetime.now(timezone.utc) - timedelta(days=age_days),
    ))
    db.commit()


def test_client_scan_does_not_become_the_vendors_score(test_db):
    """The incident shape: the newest scan on the account is a client's."""
    u = _vendor(test_db, "v+scope1@booppa.io", "https://agency.com.sg")
    _scan(test_db, u, "https://agency.com.sg", OWN_SCORE, age_days=30)
    _scan(test_db, u, "https://bigclient.com", CLIENT_SCORE, age_days=1)

    # The unscoped lookup is what shipped — kept here to show the fix changes
    # the answer rather than merely restating it.
    assert latest_pdpa_score(test_db, u.id) == CLIENT_SCORE
    assert vendor_pdpa_score(test_db, u) == OWN_SCORE


def test_www_and_scheme_differences_still_match(test_db):
    """Hostnames are normalised, so http/https and www must not orphan a scan."""
    u = _vendor(test_db, "v+scope2@booppa.io", "www.agency2.com.sg")
    _scan(test_db, u, "https://agency2.com.sg/privacy", OWN_SCORE, age_days=2)

    assert vendor_pdpa_score(test_db, u) == OWN_SCORE


def test_scan_without_a_recorded_website_is_still_accepted(test_db):
    """ReportRequest.website is optional — these rows exist and are the vendor's.

    Unattributed is not the same as contradicting: rejecting it would show
    "no PDPA scan" to a vendor who has one.
    """
    u = _vendor(test_db, "v+scope3@booppa.io", "https://agency3.com.sg")
    _scan(test_db, u, None, OWN_SCORE, age_days=2)

    assert vendor_pdpa_score(test_db, u) == OWN_SCORE


def test_contradicting_website_loses_to_unattributed_scan(test_db):
    """A client scan must be rejected even when an unattributed row is the fallback."""
    u = _vendor(test_db, "v+scope4@booppa.io", "https://agency4.com.sg")
    _scan(test_db, u, None, OWN_SCORE, age_days=30)
    _scan(test_db, u, "https://bigclient.com", CLIENT_SCORE, age_days=1)

    assert vendor_pdpa_score(test_db, u) == OWN_SCORE


def test_only_client_scans_reads_as_not_assessed(test_db):
    """No scan of the vendor's own site → None, so the surface says "not assessed".

    It must never fall through to the client's number just because one exists.
    """
    u = _vendor(test_db, "v+scope5@booppa.io", "https://agency5.com.sg")
    _scan(test_db, u, "https://bigclient.com", CLIENT_SCORE, age_days=1)

    assert vendor_pdpa_score(test_db, u) is None


def test_vendor_without_a_website_keeps_legacy_behaviour(test_db):
    """Nothing to scope by — no worse than before, and still None with no scans."""
    u = _vendor(test_db, "v+scope6@booppa.io", None)
    _scan(test_db, u, "https://somewhere.com", CLIENT_SCORE, age_days=1)

    assert vendor_pdpa_score(test_db, u) == CLIENT_SCORE


def test_no_scans_at_all_is_none(test_db):
    u = _vendor(test_db, "v+scope7@booppa.io", "https://agency7.com.sg")
    assert vendor_pdpa_score(test_db, u) is None


def test_missing_user_is_none(test_db):
    assert vendor_pdpa_score(test_db, None) is None


def test_scoped_lookup_sees_past_the_unscoped_window(test_db):
    """Scoped searches a wider window: the filter narrows, so a 5-row cap would
    let five client scans bury the vendor's own report and return None."""
    u = _vendor(test_db, "v+scope8@booppa.io", "https://agency8.com.sg")
    _scan(test_db, u, "https://agency8.com.sg", OWN_SCORE, age_days=40)
    for i in range(8):
        _scan(test_db, u, f"https://client{i}.com", CLIENT_SCORE, age_days=i + 1)

    assert vendor_pdpa_score(test_db, u) == OWN_SCORE


def test_certified_lookup_does_not_accept_unattributed(test_db):
    """Report-scoped certificates keep the strict rule.

    ``allow_unattributed`` is opt-in: a certified figure printed next to one
    named subject must be attributable to that subject.
    """
    u = _vendor(test_db, "v+scope9@booppa.io", "https://agency9.com.sg")
    _scan(test_db, u, None, OWN_SCORE, age_days=2)

    assert latest_pdpa_score(test_db, u.id, domain="https://agency9.com.sg") is None
    assert latest_pdpa_score(
        test_db, u.id, domain="https://agency9.com.sg", allow_unattributed=True
    ) == OWN_SCORE
