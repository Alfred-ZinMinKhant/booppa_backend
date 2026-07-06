"""Phase 4 copy invariants for the Vendor Proof certificate.

Two forensic findings drove these:

  1. The badge was marketed as "lifetime" while the certificate actually
     expires after 365 days — the cert must state the annual-renewal reality.
  2. Bundle buyers (Vendor Trust Pack → 2 notarization credits) had no in-doc
     reference to the credit or how to redeem it. The credit/redemption line
     must appear when — and only when — the holder actually carries credits.
"""
import io

import pypdf

from app.services.vendor_proof_generator import generate_vendor_proof_certificate


def _text(pdf_bytes: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _gen(**over) -> bytes:
    kwargs = dict(
        company_name="Acme Pte Ltd",
        uen="201812345A",
        acra_data={"matched": True, "entity_type": "Private Limited"},
        score=42,
        expires_on="06 July 2027",
    )
    kwargs.update(over)
    return generate_vendor_proof_certificate(**kwargs)


def test_cert_states_annual_renewal_not_lifetime():
    txt = _text(_gen())
    assert "renews annually" in txt
    assert "lifetime" not in txt.lower()


def test_credit_line_present_when_credits_held():
    txt = _text(_gen(notarization_credits=2))
    assert "2 notarization credit" in txt
    assert "booppa.io/notarize" in txt


def test_no_credit_line_when_zero_credits():
    # Standalone Vendor Proof grants no credits — never claim one.
    txt = _text(_gen(notarization_credits=0))
    assert "notarization credit" not in txt.lower()


def test_credit_line_singular_grammar():
    txt = _text(_gen(notarization_credits=1))
    assert "1 notarization credit" in txt
    # Body copy must read singular — "1 notarization credits" would be wrong.
    assert "1 notarization credits" not in txt
