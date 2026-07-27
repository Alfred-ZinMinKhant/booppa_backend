"""PDPC precedent classification + honest-fallback behaviour.

Covers the "PDPC enforcement precedents per finding" feature: decisions are
classified by obligation from their formulaic titles, but may only be NAMED once
a financial penalty has been read off the decision page; findings with no citable
decision fall back to an honest statutory *basis* (never labelled a precedent);
and no fabricated fine figure is ever emitted.

The last group of tests pins what the rendered sentence may claim — floors rather
than exact counts, a shared obligation rather than shared facts, and a year
labelled as the publication year.
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


def _live(monkeypatch, rows):
    import app.services.pdpc_precedents as pp
    monkeypatch.setattr(pp, "_live_precedents_for_category", lambda cat: rows)
    return pp


def test_unverified_live_rows_are_never_cited_by_name(monkeypatch):
    """A live row whose decision page yielded no penalty/year is context only.

    Regression: a delivered PDPA report cited "BHG (Singapore)" as a Notable case
    off the back of "No Breach of Protection Obligation by BHG" — an organisation
    that was cleared, not fined. A cleared decision has no financial penalty, so
    it can never carry verified=True.
    """
    bhg = {"vendor": "BHG (Singapore)", "year": None, "fine_sgd": None, "verified": False,
           "url": "https://pdpc.example/bhg", "summary": "No Breach ... by BHG"}
    fined = {"vendor": "Bar LLP", "year": 2024, "fine_sgd": 58_000, "verified": True,
             "url": "https://pdpc.example/bar", "summary": "Breach ... by Bar LLP"}
    pp = _live(monkeypatch, [bhg, fined])

    s = pp.precedent_summary("free:hsts")
    assert s and "BHG" not in s
    # Counted nowhere either: the aggregate describes only the citable set.
    assert "at least 1 decision" in s and "at least S$58k" in s
    # Still exposed for non-citation use, explicitly flagged.
    assert any(r["vendor"] == "BHG (Singapore)" and not r.get("verified")
               for r in pp.get_precedents("free:hsts"))

    # A category whose only live row is unverified has nothing citable at all.
    monkeypatch.setattr(pp, "_live_precedents_for_category", lambda cat: [bhg])
    assert pp.precedent_summary("free:hsts") is None
    assert pp.regulatory_basis("free:hsts")   # falls back to an honest basis


def test_evidence_verified_live_rows_are_cited_and_lead_with_the_recent_ones(monkeypatch):
    """Live rows verified from their decision page are citable — this is what
    keeps citations current instead of frozen at the seed. The named examples are
    the most recent, since a 2025 case reads as live risk and a 2017 one as
    history."""
    pp = _live(monkeypatch, [
        {"vendor": "Old Co", "year": 2016, "fine_sgd": 90_000, "verified": True,
         "url": "https://pdpc.example/old", "summary": "Breach ... by Old Co"},
        {"vendor": "Bar LLP", "year": 2024, "fine_sgd": 58_000, "verified": True,
         "url": "https://pdpc.example/bar", "summary": "Breach ... by Bar LLP"},
    ])
    s = pp.precedent_summary("free:no_consent_banner", max_items=1)
    assert s and "Bar LLP (2024)" in s and "Old Co" not in s
    # count and total describe the same set
    assert "at least 2 decisions" in s and "at least S$148k" in s


def test_live_index_replaces_the_seed_rather_than_stacking_on_it(monkeypatch):
    """The seed is a floor for when the scrape is down, not an addition — the
    same decision sits in both under different names/URLs, so merging would
    double-count it in the case count and the penalty total."""
    pp = _live(monkeypatch, [
        {"vendor": "SingHealth and IHiS", "year": 2019, "fine_sgd": 250_000,
         "verified": True, "url": "https://pdpc.example/singhealth",
         "summary": "Breach ... by SingHealth and IHiS"},
    ])
    assert len(pp.get_citable_precedents("free:hsts")) == 1

    # Scrape unavailable → the curated seed carries the finding.
    monkeypatch.setattr(pp, "_live_precedents_for_category", lambda cat: [])
    seeded = pp.get_citable_precedents("free:hsts")
    assert len(seeded) > 1
    assert all(c["verified"] for c in seeded)


def test_index_marks_rows_verified_only_on_page_evidence():
    """verified is earned from the decision page (penalty + citation year), not
    from the title — the title cannot say how a decision ended."""
    import asyncio
    from unittest.mock import patch
    import app.services.evidence_enricher as ee

    rows = {
        "https://pdpc.example/fined": {"fine_sgd": 12_000, "year": 2024},   # citable
        "https://pdpc.example/cleared": {"fine_sgd": None, "year": 2024},   # no penalty
        "https://pdpc.example/noyear": {"fine_sgd": 12_000, "year": None},  # unread page
    }

    async def _fake_enrich(client, url):
        return rows[url]

    listing = [(f"Breach of the Protection Obligation by Org {i}", u)
               for i, u in enumerate(rows)]

    with patch.object(ee, "_cache_get", return_value={"html": "x"}), \
         patch.object(ee, "_cache_set"), \
         patch.object(ee, "_extract_pdpc_decisions", return_value=listing), \
         patch.object(ee, "_enrich_decision_page", _fake_enrich):
        index = asyncio.run(ee.build_pdpc_precedent_index())

    prot = index["categories"]["protection"]
    assert index["citable"] == 1
    assert {r["url"] for r in prot if r["verified"]} == {"https://pdpc.example/fined"}
    assert all(not r["verified"] for r in prot if r["url"] != "https://pdpc.example/fined")


@pytest.mark.parametrize("title", [
    "No Breach of Protection Obligation by BHG (Singapore)",
    "No Further Action Against Acme for Alleged Breach of the Consent Obligation",
    "Foo Pte Ltd Not in Breach of the Protection Obligation",
    "No Financial Penalty Imposed on Bar LLP for the Protection Obligation",
])
def test_cleared_decisions_are_not_indexed(title):
    """Cleared / no-action decisions carry the same obligation words as real
    breaches; they must be excluded on outcome before the obligation rules run."""
    from app.services.evidence_enricher import classify_pdpc_title
    assert classify_pdpc_title(title) == []


def test_curated_security_header_case_is_reachable_from_a_scan_finding(monkeypatch):
    """Scan findings emit free-form `free:<type>_<title>` keys, which match no
    seed key exactly — the curated seed must still resolve, by category, or it is
    dead weight when the scrape is unavailable."""
    from app.services.pdf_service import _finding_key_from
    from app.services.pdpc_precedents import get_citable_precedents, PRECEDENTS
    import app.services.pdpc_precedents as pp
    monkeypatch.setattr(pp, "_live_precedents_for_category", lambda cat: [])

    key = _finding_key_from({"type": "security_headers",
                             "title": "Missing Security HTTP Headers"})
    cases = get_citable_precedents(key)
    assert cases, "curated protection cases must be reachable from a scan finding"
    seed_urls = {c["url"] for v in PRECEDENTS.values() for c in v}
    assert all(c["url"] in seed_urls for c in cases)
    assert any("North London Collegiate" in (c["vendor"] or "") for c in cases)


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


# ── What the rendered sentence may and may not claim ─────────────────────────

def test_summary_claims_a_shared_obligation_not_shared_facts(monkeypatch):
    """A §24 bucket spans ransomware, lost laptops and misconfigured servers, so
    "under similar facts" would overclaim. Name the obligation instead."""
    pp = _live(monkeypatch, [
        {"vendor": "Bar LLP", "year": 2024, "fine_sgd": 58_000, "verified": True,
         "section": "§24 Protection Obligation",
         "url": "https://pdpc.example/bar", "summary": "Breach ... by Bar LLP"},
    ])
    s = pp.precedent_summary("free:hsts")
    assert "similar facts" not in s
    assert "under the §24 Protection Obligation" in s


def test_summary_states_counts_and_totals_as_floors(monkeypatch):
    """Decisions under obligations we don't classify, and pages whose penalty
    couldn't be parsed, are absent — so both figures are floors, not exact."""
    pp = _live(monkeypatch, [
        {"vendor": "Bar LLP", "year": 2024, "fine_sgd": 58_000, "verified": True,
         "url": "https://pdpc.example/bar", "summary": "Breach ... by Bar LLP"},
        {"vendor": "Foo Ltd", "year": 2023, "fine_sgd": 12_000, "verified": True,
         "url": "https://pdpc.example/foo", "summary": "Breach ... by Foo Ltd"},
    ])
    s = pp.precedent_summary("free:hsts")
    assert "at least 2 decisions" in s
    assert "totalling at least S$70k" in s


def test_every_year_is_labelled_with_where_it_came_from(monkeypatch):
    """Publication can trail the decision date across a year boundary, so a
    publication year is never passed off as the year of the decision. The label
    is per case, so a mixed list stays unambiguous."""
    pp = _live(monkeypatch, [
        {"vendor": "Bar LLP", "year": 2026, "fine_sgd": 58_000, "verified": True,
         "year_source": "published",
         "url": "https://pdpc.example/bar", "summary": "Breach ... by Bar LLP"},
        {"vendor": "Foo Ltd", "year": 2023, "fine_sgd": 12_000, "verified": True,
         "year_source": "decided",
         "url": "https://pdpc.example/foo", "summary": "Breach ... by Foo Ltd"},
    ])
    s = pp.precedent_summary("free:hsts", max_items=2)
    assert "Bar LLP (published 2026)" in s
    assert "Foo Ltd (decided 2023)" in s

    # A curated entry carries a hand-checked decision year.
    monkeypatch.setattr(pp, "_live_precedents_for_category", lambda cat: [])
    assert "(decided " in pp.precedent_summary("free:hsts")


def test_a_year_with_no_known_source_is_not_labelled(monkeypatch):
    """Better an unlabelled year than a wrong label — a row from a pre-v3 cached
    index carries no year_source and must not be stamped 'published'."""
    pp = _live(monkeypatch, [
        {"vendor": "Bar LLP", "year": 2026, "fine_sgd": 58_000, "verified": True,
         "url": "https://pdpc.example/bar", "summary": "Breach ... by Bar LLP"},
    ])
    s = pp.precedent_summary("free:hsts")
    assert "Bar LLP (2026)" in s
    assert "published 2026" not in s and "decided 2026" not in s


def test_parse_decision_year_reports_its_source():
    from app.services.evidence_enricher import _parse_decision_year_with_source
    assert _parse_decision_year_with_source(
        "", "In the matter of Acme [2023] SGPDPC 4") == (2023, "decided")
    assert _parse_decision_year_with_source(
        "", '<span class="page-banner__date">Published on 18 Feb 2022</span>'
    ) == (2022, "published")
    assert _parse_decision_year_with_source("", "founded 2001, suite 2000") == (None, None)


def test_named_parties_are_cited_ahead_of_anonymised_ones(monkeypatch):
    """The register anonymises some parties ("a Registered Salesperson"). They
    still count, but a buyer cannot look them up, so a named organisation is
    cited in preference."""
    pp = _live(monkeypatch, [
        {"vendor": "a Registered Salesperson", "year": 2026, "fine_sgd": 5_000,
         "verified": True, "url": "https://pdpc.example/anon", "summary": "Breach ..."},
        {"vendor": "Acme Pte Ltd", "year": 2024, "fine_sgd": 5_000, "verified": True,
         "url": "https://pdpc.example/acme", "summary": "Breach ... by Acme"},
    ])
    s = pp.precedent_summary("free:hsts", max_items=1)
    assert "Acme Pte Ltd (2024)" in s and "Registered Salesperson" not in s
    assert "at least 2 decisions" in s      # both still counted


def test_breach_notification_finding_does_not_cite_protection_cases():
    """§26A-D notification and §24 protection are different duties. The rendered
    line names the obligation it cites, so a mis-bucket reads as a claim about
    the wrong duty."""
    from app.services.pdf_service import _finding_key_from
    from app.services.pdpc_precedents import finding_category

    key = _finding_key_from({"type": "breach_notification",
                             "title": "No breach notification procedure"})
    assert finding_category(key) == "notification"
    # A finding about prior enforcement history still maps to protection.
    hist = _finding_key_from({"type": "data_breach", "title": "Prior data breach on record"})
    assert finding_category(hist) == "protection"


@pytest.mark.parametrize("finding_type,expected", [
    ("transfer", "transfer"),
    ("accuracy", "accuracy"),
    ("breach_notification", "notification"),
])
def test_every_indexed_category_is_reachable_from_a_finding(finding_type, expected):
    """A category the index populates but no finding can reach renders neither a
    precedent nor a basis — a silent hole rather than a visible one."""
    from app.services.pdf_service import _finding_key_from
    from app.services.pdpc_precedents import finding_category, regulatory_basis

    key = _finding_key_from({"type": finding_type, "title": f"{finding_type} issue"})
    assert finding_category(key) == expected
    assert regulatory_basis(key)
