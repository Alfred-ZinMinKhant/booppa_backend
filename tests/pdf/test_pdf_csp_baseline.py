"""CSP Registration Readiness Baseline PDF — the Day-1 artifact.

Hermetic: the generator takes a plain dict, so nothing here touches ACRA, S3, or
Stripe. Assertions run against the ReportLab flowables rather than pypdf
`extract_text()` — table pagination shifts cells across page breaks and
extraction silently drops them.
"""

from app.services.csp_baseline_generator import (
    CSP_BASELINE_SCHEMA_VERSION,
    generate_csp_baseline_pdf,
)


_ACRA_LIVE = {
    "found": True,
    "live": True,
    "uen": "201912345A",
    "registered_name": "ACME CORPORATE SERVICES PTE. LTD.",
    "entity_type": "LOCAL COMPANY",
    "entity_status": "Live Company",
    "registration_date": "2019-04-01",
}

_PROVISIONING = [
    {"capability": "CSP compliance workspace", "status": "Active", "detail": "Active"},
    {"capability": "Blockchain evidence ledger", "status": "Active", "detail": "On-chain"},
]


def _data(**over):
    base = {
        "company_name": "Acme Corporate Services Pte Ltd",
        "website": "https://acme.example",
        "plan_label": "CSP Compliance Pack — Full",
        "billing_label": "One-time purchase",
        "acra": _ACRA_LIVE,
        "provisioning": _PROVISIONING,
    }
    base.update(over)
    return base


def _cell_texts(monkeypatch, data) -> list[str]:
    """Render, capturing every Table cell's Paragraph text along the way."""
    from reportlab.platypus import Table

    seen: list[str] = []
    orig_init = Table.__init__

    def _spy(self, cellvalues, *a, **kw):
        for row in cellvalues or []:
            for cell in row:
                text = getattr(cell, "text", None)
                if isinstance(text, str):
                    seen.append(text)
                elif isinstance(cell, str):
                    seen.append(cell)
        return orig_init(self, cellvalues, *a, **kw)

    monkeypatch.setattr(Table, "__init__", _spy)
    pdf = generate_csp_baseline_pdf(data)
    assert pdf.startswith(b"%PDF"), "generator must emit a real PDF"
    assert len(pdf) > 5000
    return seen


def test_renders_acra_legal_name_not_the_raw_domain(monkeypatch):
    """The 'Assessed Entity: thunes.com' bug class — the verified-entity block
    must carry the ACRA registered name and UEN."""
    cells = _cell_texts(monkeypatch, _data())
    joined = " ".join(cells)
    assert "ACME CORPORATE SERVICES PTE. LTD." in joined
    assert "201912345A" in joined
    assert not any(c.strip() == "acme.example" for c in cells)


def test_ampersand_in_company_name_survives_xml_escape(monkeypatch):
    """ReportLab Paragraph mini-XML treats & as an entity start — an unescaped
    name raises or renders mangled (the 'Q&A Coverage' glitch)."""
    data = _data(
        company_name="Acme & Sons Pte Ltd",
        acra={**_ACRA_LIVE, "registered_name": "ACME & SONS PTE. LTD."},
    )
    cells = _cell_texts(monkeypatch, data)
    assert any("&amp;" in c for c in cells), "the & must be escaped, not dropped"
    assert not any("& SONS" in c and "&amp;" not in c for c in cells)


def _paragraph_texts(monkeypatch, data) -> list[str]:
    """Every Paragraph string passed to the builder, table cells included."""
    from reportlab.platypus import Paragraph

    seen: list[str] = []
    orig = Paragraph.__init__

    def _spy(self, text, *a, **kw):
        if isinstance(text, str):
            seen.append(text)
        return orig(self, text, *a, **kw)

    monkeypatch.setattr(Paragraph, "__init__", _spy)
    generate_csp_baseline_pdf(data)
    return seen


def test_does_not_claim_to_be_the_amlcft_programme(monkeypatch):
    """Layer 1 of the readiness gate: the document must not overstate itself.
    This is a Day-1 baseline, not the programme and not a compliance statement."""
    body = " ".join(_paragraph_texts(monkeypatch, _data())).lower()

    assert "not a statement of compliance" in body
    assert "corporate service providers act 2024" in body
    # The fit-and-proper / STR assessments are the CSP's own to perform.
    assert "fit and proper" in body or "fit-and-proper" in body
    assert "str" in body


def test_unverified_entity_is_stated_plainly(monkeypatch):
    """A registry miss must be disclosed, never papered over — an ACRA outage
    still yields an artifact, but one that says the entity is unconfirmed."""
    body = " ".join(_paragraph_texts(monkeypatch, _data(acra={"found": False}))).lower()
    assert "not confirmed against the acra register" in body


def test_struck_off_entity_is_disclosed(monkeypatch):
    cells = _cell_texts(monkeypatch, _data(acra={
        **_ACRA_LIVE, "live": False, "entity_status": "Struck Off",
    }))
    assert any("Struck Off" in c for c in cells)


def test_schema_version_is_stamped(monkeypatch):
    texts = _paragraph_texts(monkeypatch, _data())
    assert any(f"v{CSP_BASELINE_SCHEMA_VERSION}" in t for t in texts)
