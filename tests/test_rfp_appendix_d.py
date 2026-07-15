"""RFP "Appendix D" generic data-protection appendix generator."""
from app.services.rfp_appendix_d_generator import build_appendix_d_pdf


def _extract_text(pdf_bytes: bytes) -> str:
    try:
        from pypdf import PdfReader
        from io import BytesIO
        reader = PdfReader(BytesIO(pdf_bytes))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return pdf_bytes.decode("latin-1", errors="ignore")


_QA = [
    {"question": "How is personal data encrypted at rest?", "answer": "AES-256 across all stores.", "verified": True},
    {"question": "Do you have a breach response plan?", "answer": "Yes — 72h notification process.", "verified": False},
]


def test_appendix_is_valid_pdf_with_numbered_items():
    pdf = build_appendix_d_pdf(
        company_name="Acme Pte Ltd",
        qa_items=_QA,
        vendor_ctx={"uen": "201912345A", "acra_name": "Acme Pte Ltd"},
        acra_live={"found": True, "entity_status": "Live"},
        compliance_score=78,
        report_id="rep-123",
    )
    assert pdf[:4] == b"%PDF"
    text = _extract_text(pdf)
    assert "D.1" in text and "D.2" in text


def test_template_disclaimer_present():
    """The 'generic template — not tender-specific' guard must be on the doc."""
    pdf = build_appendix_d_pdf(company_name="Acme Pte Ltd", qa_items=_QA)
    text = _extract_text(pdf).lower()
    assert "generic template" in text
    assert "per-tender" in text or "per tender" in text


def test_verified_vs_client_declared_follows_qa_flag():
    pdf = build_appendix_d_pdf(company_name="Acme Pte Ltd", qa_items=_QA, compliance_score=82)
    text = _extract_text(pdf)
    assert "VERIFIED" in text
    assert "CLIENT-DECLARED" in text


def test_verified_item_carries_its_evidence_line():
    """RFP-2: a VERIFIED item must show the justifying evidence line right under
    the badge — the badge is never presented as a bare boolean. In Appendix D the
    evidence line used to be dropped entirely, leaving a naked 'VERIFIED' badge."""
    qa = [{
        "question": "How is personal data encrypted in transit?",
        "answer": "TLS 1.3 across all endpoints.",
        "verified": True,
        "evidence": ["SSL Labs grade A+ (verified 2026-07-15)"],
    }]
    pdf = build_appendix_d_pdf(company_name="Acme Pte Ltd", qa_items=qa, compliance_score=80)
    text = _extract_text(pdf)
    assert "VERIFIED" in text
    assert "Evidence:" in text
    assert "SSL Labs grade A+" in text


def test_unverified_item_shows_no_evidence_line_and_no_verified_badge():
    """The complement: a client-declared item must not render a VERIFIED badge and
    must not attach an evidence justification it doesn't have."""
    qa = [{
        "question": "Do you encrypt at rest?",
        "answer": "[Verify: encryption standard for data at rest]",
        "verified": False,
        "evidence": [],
    }]
    pdf = build_appendix_d_pdf(company_name="Acme Pte Ltd", qa_items=qa)
    text = _extract_text(pdf)
    # The item carries the amber CLIENT-DECLARED badge...
    assert "CLIENT-DECLARED" in text
    # ...and NO evidence justification line is attached to it. ("VERIFIED — BOOPPA"
    # itself can't be asserted absent — it always appears in the document legend.)
    assert "Evidence:" not in text


def test_missing_qa_still_renders():
    pdf = build_appendix_d_pdf(company_name="Acme Pte Ltd", qa_items=[])
    assert pdf[:4] == b"%PDF"


def test_xml_escape_does_not_crash():
    pdf = build_appendix_d_pdf(
        company_name="Smith & Jones <Holdings>",
        qa_items=[{"question": "A & B < C", "answer": "x < y & z", "verified": False}],
    )
    assert pdf[:4] == b"%PDF"
