"""The CSP firm's own baseline must read its cached UEN through the trust gate.

When a CSP order names no `managed_entity_id`, the account genuinely IS the subject
— the CSP Compliance Pack's baseline is about the firm's own registration readiness.
That legitimacy is exactly what made this branch easy to leave alone, and it was the
last place in the CSP path still reading `user.uen` raw.

A raw read is the SPQR shape: a stale-but-real UEN survives an account's company
change, `fetch_acra_status` matches it against the registry, and the baseline is
issued under a genuine ACRA record for a company the firm no longer is. The document
looks entirely correct.

So the property pinned here is about what reaches `fetch_acra_status`: the cached UEN
when provenance matches the subject, and `None` — falling through to a name-only
lookup — when it does not.
"""
from __future__ import annotations

import uuid

import pytest

from app.core.models import User


@pytest.fixture
def acra_calls(monkeypatch):
    """Record the (uen, company) every baseline run asks the registry about."""
    calls: list[tuple] = []

    async def _fake(uen=None, company=None, *a, **kw):
        calls.append((uen, company))
        return {"found": False}

    monkeypatch.setattr("app.services.evidence_enricher.fetch_acra_status", _fake)
    return calls


@pytest.fixture
def stub_delivery(monkeypatch):
    """Neutralise PDF/S3/email so the test exercises identity resolution only."""
    monkeypatch.setattr(
        "app.services.csp_baseline_generator.generate_csp_baseline_pdf",
        lambda *a, **kw: b"%PDF-stub",
    )

    class _S3:
        async def upload_pdf(self, *a, **kw):
            return "https://s3.example/csp-baseline.pdf"

    class _Email:
        async def send_html_email(self, *a, **kw):
            return True

    monkeypatch.setattr("app.services.storage.S3Service", lambda *a, **kw: _S3())
    monkeypatch.setattr("app.services.email_service.EmailService", lambda *a, **kw: _Email())


def _csp_user(db, *, company, website, uen, legal_name_hint, website_hint) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex[:12]}@example.com",
        hashed_password="x",
        role="VENDOR",
        is_active=True,
        company=company,
        website=website,
        uen=uen,
        legal_name=company,
        legal_name_hint=legal_name_hint,
        website_hint=website_hint,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _run(user_id, **kw):
    from app.workers.csp_tasks import run_csp_baseline_for_user

    return run_csp_baseline_for_user(
        user_id=str(user_id), plan="csp", billing_type="one_time", **kw
    )


def test_matching_provenance_lets_the_firm_use_its_own_uen(
    test_db, acra_calls, stub_delivery
):
    """The ordinary case: the firm is the subject and its cached identity was
    recorded for this same company, so it is trustworthy here."""
    user = _csp_user(
        test_db,
        company="BOOPPA CORPORATE SERVICES PTE. LTD.",
        website="https://booppa-cs.example",
        uen="199900002B",
        legal_name_hint="BOOPPA CORPORATE SERVICES PTE. LTD.",
        website_hint="https://booppa-cs.example",
    )

    _run(user.id)

    assert acra_calls, "the baseline never consulted ACRA"
    assert acra_calls[-1][0] == "199900002B"


def test_a_different_named_subject_does_not_reach_the_registry(
    test_db, acra_calls, stub_delivery
):
    """Behaviour the old string-comparison guard already had, pinned so the move to
    `trusted_cached_uen` cannot quietly lose it. The run names a company that is not this account's, so
    the account's cached UEN belongs to a different legal person and must never be
    matched against ACRA on that subject's behalf — feeding it in returns a real,
    correct registry record for the wrong entity, which is the whole SPQR shape."""
    user = _csp_user(
        test_db,
        company="BOOPPA CORPORATE SERVICES PTE. LTD.",
        website="https://booppa-cs.example",
        uen="199900002B",
        legal_name_hint="BOOPPA CORPORATE SERVICES PTE. LTD.",
        website_hint="https://booppa-cs.example",
    )

    _run(
        user.id,
        override_company="NETPOLEONS TECHNOLOGY PTE LTD",
        override_website="https://netpoleons.example",
    )

    assert acra_calls, "the baseline never consulted ACRA"
    sent_uen, sent_company = acra_calls[-1]
    assert sent_uen is None, (
        "the account's own UEN was sent to the registry under another company's name"
    )
    assert "NETPOLEONS" in (sent_company or ""), (
        "with no trusted UEN the lookup must still carry the named subject"
    )


def test_unprovenanced_uen_is_not_trusted(test_db, acra_calls, stub_delivery):
    """The load-bearing one: this is the case the old raw read let through. A UEN
    with no recorded provenance at all — a backfill, an import, a row predating the
    provenance columns — has nothing establishing which company it was confirmed for,
    so it cannot be vouched for even though the account is its own subject here."""
    user = _csp_user(
        test_db,
        company="BOOPPA CORPORATE SERVICES PTE. LTD.",
        website="https://booppa-cs.example",
        uen="199900002B",
        legal_name_hint=None,
        website_hint=None,
    )

    _run(user.id)

    assert acra_calls[-1][0] is None
