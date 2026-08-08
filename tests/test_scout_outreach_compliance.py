"""
tests/test_scout_outreach_compliance.py

Regression guard for AUDIT_2026-08-07.md P0-3.

SCOUT sends unsolicited commercial email. Before 2026-08-07 every message it
produced breached the Spam Control Act (Cap. 311A), Second Schedule: no "<ADV>"
subject prefix (para 3), no unsubscribe facility in the message body (para 2(a)
— only an SMTP List-Unsubscribe header, which a reader cannot act on), and no
sender postal address (para 2(b)). It also shipped the literal "Dear [Name],"
and asserted PDPA gaps the scan had not detected.

These tests cover both halves of the fix: the generator, and the send-side gate
that stops non-compliant bodies already stored in the database from going out.
A failure here is a legal exposure, not a cosmetic one.
"""

import pytest

from app.core.config import settings
from app.services.email_layout import adv_subject, is_adv_subject
from app.services.email_suppression import unsubscribe_url
from app.services import scout_outreach_templates as templates
from app.workers.scout_celery_tasks import _outreach_compliance_error

POSTAL = "Booppa Smart Care LLC, 68 Circular Road #02-01, Singapore 049422"
RECIPIENT = "ops@acme.example"


@pytest.fixture
def postal_address(monkeypatch):
    monkeypatch.setattr(settings, "COMPANY_POSTAL_ADDRESS", POSTAL)
    return POSTAL


def _vendor_data(**over):
    data = {
        "clean_name": "Acme Logistics Pte Ltd",
        "trust_score": 41,
        "sector": "logistics",
        "priority_tier": "TIER1",
        "contact_email": RECIPIENT,
        "findings": {
            "cookie_banner": {"present": False},
            "privacy_policy": {"present": False},
            "dpo_contact": {"present": True},
        },
    }
    data.update(over)
    return data


def _all_generators(data):
    """The same message obligations apply to all three pipelines."""
    return [
        templates.generate_vendor_outreach(data, "Top Vendor", 88),
        templates.generate_buyer_outreach(
            {**data, "fit_tier": "BUYER_ESSENTIALS_FIT", "agency_name": "Some Agency",
             "award_count": 12, "distinct_vendors": 7}
        ),
        templates.generate_csp_outreach({**data, "aml_readiness_score": 44}),
    ]


# ── Generator ──────────────────────────────────────────────────────────────

def test_adv_prefix_is_idempotent():
    assert adv_subject("Hello") == "<ADV> Hello"
    assert adv_subject("<ADV> Hello") == "<ADV> Hello"
    assert is_adv_subject(adv_subject("Hello"))


def test_all_pipelines_produce_compliant_messages(postal_address):
    for subject, body in _all_generators(_vendor_data()):
        assert subject and body
        assert is_adv_subject(subject)
        assert unsubscribe_url(RECIPIENT) in body
        assert POSTAL in body
        assert "[Name]" not in body


def test_generators_refuse_without_recipient_address(postal_address):
    """No address means no unsubscribe link can be built, so nothing renders."""
    for subject, body in _all_generators(_vendor_data(contact_email="")):
        assert (subject, body) == ("", "")


def test_generators_refuse_without_postal_address(monkeypatch):
    """Fails closed: COMPANY_POSTAL_ADDRESS has no safe default."""
    monkeypatch.setattr(settings, "COMPANY_POSTAL_ADDRESS", "")
    for subject, body in _all_generators(_vendor_data()):
        assert (subject, body) == ("", "")


def test_only_detected_gaps_are_named(postal_address):
    """The padding that invented gaps is gone — see absence_of_evidence rule."""
    _, body = templates.generate_vendor_outreach(_vendor_data(), "", 0)
    assert "Cookie Consent Banner" in body
    assert "Privacy Policy Present" in body
    assert "a PDPA compliance gap" not in body
    assert "your data protection policy" not in body


def test_clean_scan_asserts_no_gaps_and_no_gap_upsell(postal_address):
    """All checks passing must produce no gap section and no 'gaps found' upsell."""
    clean = _vendor_data(findings={k: {"present": True} for k in
                                   ("cookie_banner", "privacy_policy", "dpo_contact")})
    _, body = templates.generate_vendor_outreach(clean, "", 0)
    assert "urgent gap" not in body
    assert "Given the number of gaps found" not in body


def test_single_gap_uses_singular_heading(postal_address):
    one = _vendor_data(findings={
        "cookie_banner": {"present": False},
        "privacy_policy": {"present": True},
        "dpo_contact": {"present": True},
    })
    _, body = templates.generate_vendor_outreach(one, "", 0)
    assert "The most urgent gap identified" in body
    assert "The two most urgent gaps" not in body


# ── Send-side gate ─────────────────────────────────────────────────────────

def _good_body(postal=POSTAL):
    return f'<p>Hi</p><a href="{unsubscribe_url(RECIPIENT)}">unsubscribe</a><p>{postal}</p>'


def test_gate_passes_a_compliant_message(postal_address):
    assert _outreach_compliance_error("<ADV> Hello", _good_body(), RECIPIENT) is None


@pytest.mark.parametrize(
    "subject,body,expect",
    [
        ("Hello", _good_body(), "<ADV>"),
        ("<ADV> Hello", '<p>Hi</p><p>' + POSTAL + '</p>', "unsubscribe"),
        ("<ADV> Hello", f'<a href="{unsubscribe_url(RECIPIENT)}">u</a>', "postal address"),
        ("<ADV> Hello", _good_body() + "Dear [Name],", "[Name]"),
    ],
    ids=["no-adv-prefix", "no-unsubscribe", "no-postal-address", "unfilled-placeholder"],
)
def test_gate_blocks_non_compliant_messages(postal_address, subject, body, expect):
    problem = _outreach_compliance_error(subject, body, RECIPIENT)
    assert problem is not None and expect in problem


def test_gate_blocks_pre_fix_bodies(postal_address):
    """Rows APPROVED before the fix are still in the queue with old bodies.

    This is the case the generator fix cannot reach: the body was rendered and
    stored months ago. The gate reads the stored artefact, so it catches them.
    """
    old_subject = "Acme Logistics Pte Ltd — Trust Score 41/100 on the GeBIZ Vendor Index"
    old_body = "<p>Dear [Name],</p><p>I recently completed an automated PDPA scan.</p>"
    assert _outreach_compliance_error(old_subject, old_body, RECIPIENT) is not None


def test_send_task_aborts_without_burning_the_queue(monkeypatch):
    """An unset postal address must stop the batch, not fail every prospect.

    Marking rows SEND_FAILED over one missing setting would require manually
    resetting the whole queue once it is configured.
    """
    from app.workers import scout_celery_tasks as tasks

    monkeypatch.setattr(settings, "COMPANY_POSTAL_ADDRESS", "")

    def _boom():  # the task must return before it ever opens a session
        raise AssertionError("SessionLocal opened despite missing postal address")

    monkeypatch.setattr(tasks, "SessionLocal", _boom)
    assert tasks.scout_send_approved_batch_task() is None


def test_gate_blocks_unsubscribe_link_for_a_different_recipient(postal_address):
    """A body carrying someone else's token gives this recipient no opt-out."""
    body = _good_body().replace(unsubscribe_url(RECIPIENT), unsubscribe_url("other@x.example"))
    problem = _outreach_compliance_error("<ADV> Hello", body, RECIPIENT)
    assert problem is not None and "unsubscribe" in problem
