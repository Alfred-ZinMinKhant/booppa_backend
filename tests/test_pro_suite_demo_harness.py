"""Pro Suite activation harness — the four capabilities must actually flip to Active.

These tests exist because the failure that blocked commercial sign-off was not a
crash: every feature was built, and the baseline still rendered them "Ready". So
what's asserted here is *activation and its artifacts*, not merely that the
harness ran without raising.

Re-run safety matters as much as the first run: QA clicks these twice, and
`initialise_trm_controls` has no idempotency guard.
"""
import pytest

from app.core.db import SessionLocal
from app.core.models import (
    MAS_TRM_DOMAINS, Organisation, SsoConfig, TrmControl, User, WhiteLabelConfig,
)
from app.services.pro_suite_demo_harness import activate_pro_features, run_sso_roundtrip
from app.services.saml_mock_idp import xmlsec1_available

COMPANY = "PytestPay Fintech Pte Ltd"
EMAIL = "pro-suite-harness@pytest.booppa.io"


@pytest.fixture(scope="module")
def activation():
    """One activation shared across the module — it runs the real PDF worker."""
    db = SessionLocal()
    result = None
    try:
        result = activate_pro_features(
            customer_email=EMAIL, company_name=COMPANY,
            live_ai=False, capture_pdf=True, db=db,
        )
        yield result, db
    finally:
        if result_dir := (result or {}).get("mock_idp_dir"):
            import shutil
            shutil.rmtree(result_dir, ignore_errors=True)
        db.close()


def test_two_subsidiaries_are_linked_with_different_profiles(activation):
    """Uneven on purpose — a rollup where every entity looks identical
    demonstrates nothing about the rollup."""
    result, db = activation
    subs = result["subsidiaries"]
    assert len(subs) == 2

    linked = db.query(User).filter(User.parent_user_id == result["user_id"]).all()
    assert {str(u.id) for u in linked} == {s["id"] for s in subs}

    completions = {s["domains_complete"] for s in subs}
    assert len(completions) == 2, "both subsidiaries have the same completion count"
    assert all(s["domains_total"] == len(MAS_TRM_DOMAINS) for s in subs)


def test_each_subsidiary_has_exactly_thirteen_controls(activation):
    """`initialise_trm_controls` is not idempotent — a second call yields 26 rows."""
    result, db = activation
    for sub in result["subsidiaries"]:
        count = (
            db.query(TrmControl)
            .filter(TrmControl.organisation_id == sub["org_id"])
            .count()
        )
        assert count == len(MAS_TRM_DOMAINS), f"{sub['name']} has {count} controls"


def test_reported_completion_matches_the_database(activation):
    """The number printed in the rollup must be the number of compliant rows."""
    result, db = activation
    for sub in result["subsidiaries"]:
        actual = (
            db.query(TrmControl)
            .filter(
                TrmControl.organisation_id == sub["org_id"],
                TrmControl.status == "compliant",
            )
            .count()
        )
        assert actual == sub["domains_complete"]


def test_white_label_config_is_persisted(activation):
    result, db = activation
    wl = (
        db.query(WhiteLabelConfig)
        .filter(WhiteLabelConfig.organisation_id == result["org_id"])
        .first()
    )
    assert wl is not None
    assert wl.report_header_text == result["white_label"]["report_header_text"]
    assert wl.primary_color and wl.secondary_color


def test_sso_config_is_active_saml(activation):
    result, db = activation
    cfg = (
        db.query(SsoConfig)
        .filter(SsoConfig.organisation_id == result["org_id"])
        .first()
    )
    assert cfg is not None
    assert cfg.protocol == "saml"
    assert cfg.is_active is True
    assert cfg.sp_acs_url and cfg.sp_acs_url.endswith(f"/acs/{result['org_slug']}")


def test_all_four_capabilities_report_active(activation):
    """The whole point: nothing may still read "Ready" after activation."""
    result, _ = activation
    assert result["provisioning_status"] == {
        "multi_subsidiary": "Active",
        "white_label": "Active",
        "sso": "Active",
        "notarizations": "Active",
    }


def test_branded_pdf_carries_the_customer_brand_and_not_ours(activation):
    """White-label means our name is gone, not just that the colours changed."""
    pytest.importorskip("pypdf")
    from pypdf import PdfReader
    from io import BytesIO

    result, _ = activation
    pdf = result.get("pdf_bytes")
    assert pdf, "harness produced no PDF"

    text = "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf)).pages)
    # Line wrapping in table cells breaks long names, so compare on collapsed text.
    flat = " ".join(text.split())

    assert result["white_label"]["report_header_text"] in flat
    assert "Booppa" not in flat, "the branded report still names Booppa"
    assert "Group Subsidiary Rollup" in flat
    for sub in result["subsidiaries"]:
        assert sub["name"] in flat, f"{sub['name']} missing from the rollup"


def test_rerun_does_not_create_a_third_subsidiary(activation):
    """QA clicks the demo button twice; that must not fork new tenants."""
    result, db = activation
    before = {s["id"] for s in result["subsidiaries"]}

    again = activate_pro_features(
        customer_email=EMAIL, company_name=COMPANY,
        live_ai=False, capture_pdf=True, with_mock_idp=False, db=db,
    )
    assert {s["id"] for s in again["subsidiaries"]} == before
    assert db.query(User).filter(User.parent_user_id == result["user_id"]).count() == 2
    for sub in again["subsidiaries"]:
        assert (
            db.query(TrmControl)
            .filter(TrmControl.organisation_id == sub["org_id"])
            .count()
        ) == len(MAS_TRM_DOMAINS)


@pytest.mark.skipif(not xmlsec1_available(), reason="xmlsec1 not installed")
def test_signed_assertion_authenticates_against_the_real_acs_route(activation):
    """End-to-end SSO: signature validation, JIT provisioning and token minting,
    through the real route rather than a mocked service call."""
    result, db = activation
    out = run_sso_roundtrip(user_id=result["user_id"], db=db)
    assert out["ok"], out.get("error")
    assert out["assertion_valid"], "no session tokens were minted"
    assert out["status_code"] == 302

    provisioned = db.query(User).filter(User.email == out["name_id"]).first()
    assert provisioned is not None, "the SSO user was not JIT-provisioned"


@pytest.mark.skipif(not xmlsec1_available(), reason="xmlsec1 not installed")
def test_tampered_assertion_is_refused_with_401(activation):
    """A rejected login is the caller's fault, not a server fault — 401, not 500."""
    result, db = activation
    out = run_sso_roundtrip(user_id=result["user_id"], tamper=True, db=db)
    assert out["ok"] is False
    assert out["status_code"] == 401, f"expected 401, got {out['status_code']}"
