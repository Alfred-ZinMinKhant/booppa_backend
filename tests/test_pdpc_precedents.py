"""PDPC precedent classification + honest-fallback behaviour.

Covers the "PDPC enforcement precedents per finding" feature: real published
decisions are classified by obligation from their formulaic titles, findings with
no real decision on file fall back to an honest statutory *basis* (never labelled a
precedent), and no fabricated fine figure is ever emitted.
"""
import pytest


@pytest.mark.parametrize("title,expected", [
    ("Breach of the Protection Obligation by Acme Pte Ltd", {"protection"}),
    ("Breach of the Protection and Openness Obligations by Foo Ltd", {"protection", "openness_dpo"}),
    ("Failure to Appoint a Data Protection Officer by Bar LLP", {"openness_dpo"}),
    ("Breach of the Consent Obligation by Baz", {"consent"}),
    ("Breach of the Do Not Call Provisions by Qux", {"dnc"}),
    ("Directions imposed for NRIC disclosure by Quux", {"nric"}),
    ("Breach of the Retention Limitation Obligation by Zed", {"retention"}),
    ("Some unrelated news headline about compliance training", set()),
])
def test_classify_pdpc_title(title, expected):
    from app.services.evidence_enricher import classify_pdpc_title
    assert set(classify_pdpc_title(title)) == expected


def test_vendor_extracted_from_title():
    from app.services.evidence_enricher import _vendor_from_title
    assert _vendor_from_title("Breach of the Consent Obligation by Baz Pte Ltd") == "Baz Pte Ltd"
    assert _vendor_from_title("Breach of the Protection Obligation by Acme [2023] SGPDPC 4") == "Acme"


# ── PDPC listing extraction (site is now a JSON island, not <a> tags) ─────────

# The live "All Commission's Decisions" page embeds each decision as a
# backslash-escaped JSON object; the old <a>-tag scraper found zero rows against
# it (and the old /all-enforcement-decisions URL 404s). This fixture mirrors the
# real escaped-island shape.
_ISLAND_HTML = (
    r'<script>var m={"items":['
    r'{\"label\":\"Breach of the Protection Obligation by AIG\",'
    r'\"url\":\"/organisations/regulations-decisions/enforcement-decisions/breach-of-protection-obligation-by-aig\",'
    r'\"isListingDetail\":true},'
    r'{\"label\":\"Breach of the Consent Obligation by Baz Pte Ltd\",'
    r'\"url\":\"/organisations/regulations-decisions/enforcement-decisions/breach-of-consent-obligation-by-baz\",'
    r'\"isListingDetail\":true},'
    r'{\"label\":\"Breach of the Protection Obligation by AIG\",'   # duplicate URL
    r'\"url\":\"/organisations/regulations-decisions/enforcement-decisions/breach-of-protection-obligation-by-aig\",'
    r'\"isListingDetail\":true}'
    r']};</script>'
)


def test_extract_pdpc_decisions_parses_json_island_and_dedupes():
    from app.services.evidence_enricher import _extract_pdpc_decisions
    rows = _extract_pdpc_decisions(_ISLAND_HTML)
    urls = [u for _, u in rows]
    titles = [t for t, _ in rows]
    # Two unique decisions (duplicate URL collapsed), absolute URLs.
    assert len(rows) == 2
    assert "Breach of the Protection Obligation by AIG" in titles
    assert all(u.startswith("https://www.pdpc.gov.sg/organisations/") for u in urls)


def test_extract_pdpc_decisions_anchor_tag_fallback():
    """If the site ever reverts to server-rendered anchors, the fallback path
    still yields decisions."""
    from app.services.evidence_enricher import _extract_pdpc_decisions
    html = (
        '<a href="/organisations/regulations-decisions/enforcement-decisions/'
        'breach-of-protection-obligation-by-acme">Breach of the Protection '
        'Obligation by Acme Pte Ltd</a>'
    )
    rows = _extract_pdpc_decisions(html)
    assert len(rows) == 1
    assert rows[0][0].startswith("Breach of the Protection Obligation by Acme")
    assert rows[0][1].endswith("/breach-of-protection-obligation-by-acme")


def test_extract_pdpc_decisions_empty_on_unrelated_html():
    from app.services.evidence_enricher import _extract_pdpc_decisions
    assert _extract_pdpc_decisions("<html><body>no decisions here</body></html>") == []


def test_finding_key_maps_to_category():
    from app.services.pdpc_precedents import finding_category
    assert finding_category("free:no_consent_banner") == "consent"
    assert finding_category("free:no_dpo_contact") == "openness_dpo"
    assert finding_category("free:hsts") == "protection"
    assert finding_category("free:nric_exposure") == "nric"
    # substring fallback for AI-generated / DNC finding types
    assert finding_category("free:dnc_registry_missing") == "dnc"
    # genuinely unknown → None
    assert finding_category("free:some_unknown_thing") is None


def test_regulatory_basis_is_offered_when_no_precedent(monkeypatch):
    """A finding whose type maps to an obligation but has no real decision on
    file must get an honest statutory basis — and no precedent summary."""
    import app.services.pdpc_precedents as pp
    # Force an empty live index so nothing but the static seed applies.
    monkeypatch.setattr(pp, "_live_precedents_for_category", lambda cat: [])

    # no_consent_banner is not in the static seed → no precedent, but has a basis
    assert pp.precedent_summary("free:no_consent_banner") is None
    basis = pp.regulatory_basis("free:no_consent_banner")
    assert basis and "Consent" in basis

    # unknown finding type → neither
    assert pp.regulatory_basis("free:some_unknown_thing") is None


def test_static_seed_still_yields_precedent():
    """The human-verified static breach seed must still produce a precedent
    summary (backward compatibility)."""
    from app.services.pdpc_precedents import precedent_summary
    s = precedent_summary("breach:pdpc_enforcement")
    assert s and "PDPC" in s


def test_precedent_summary_never_prints_fake_zero_total(monkeypatch):
    """Live rows may carry fine_sgd=None. The summary must not claim 'S$0' — it
    should describe the decisions without inventing a penalty figure."""
    import app.services.pdpc_precedents as pp
    fake = [
        {"vendor": "Foo Ltd", "year": None, "fine_sgd": None,
         "url": "https://pdpc.example/foo", "summary": "Breach ... by Foo Ltd"},
        {"vendor": "Bar LLP", "year": 2022, "fine_sgd": None,
         "url": "https://pdpc.example/bar", "summary": "Breach ... by Bar LLP"},
    ]
    monkeypatch.setattr(pp, "_live_precedents_for_category", lambda cat: fake)
    s = pp.precedent_summary("free:no_consent_banner")
    assert s is not None
    assert "S$0" not in s and "$0" not in s
    assert "2 enforcement decisions" in s
    assert "Foo Ltd" in s  # named without a year


# ── Decision-year parsing (only the neutral-citation year is trusted) ─────────

def test_decision_year_from_neutral_citation():
    """The decision year comes from the '[YYYY] SGPDPC N' citation, not the
    first stray 20xx on the page."""
    from app.services.evidence_enricher import _parse_decision_year
    text = (
        "Registered address: 2000 Bendemeer Road. In the matter of Acme Pte Ltd "
        "[2023] SGPDPC 4. A financial penalty of S$5,000 was imposed."
    )
    # 2000 (an address) must NOT win — the citation year 2023 must.
    assert _parse_decision_year("https://pdpc.gov.sg/…/breach-by-acme", text) == 2023


def test_decision_year_none_when_no_citation():
    """No citation → None (case is named without a year), never a fabricated one."""
    from app.services.evidence_enricher import _parse_decision_year
    # A page full of 20xx numbers but no SGPDPC citation must not yield a year.
    assert _parse_decision_year("https://pdpc.gov.sg/x", "founded 2001, suite 2000") is None


def test_decision_year_rejects_pre_enforcement_year():
    """PDPA enforcement is 2013+; a citation year before that is not credible."""
    from app.services.evidence_enricher import _parse_decision_year
    assert _parse_decision_year("", "In the matter of Foo [2000] SGPDPC 1") is None
