"""Unit tests for the offline ACRA seed normalizer (`acra_service._normalize`).

The offline refresh pulls the real ACRA business-entities dataset from data.gov.sg
and upserts LIVE entities into `discovered_vendors`. `_normalize` decides which raw
datastore records become rows and how their fields map. These tests are pure (no
network, no DB) — they pin the accept/skip policy and the field mapping so the seed
mirrors what the live lookup would report as ``live=True``.
"""
from app.services import acra_service as acr


def _rec(**over):
    base = {
        "uen": "201812345A",
        "entity_name": "ACME PTE. LTD.",
        "entity_type_desc": "LOCAL COMPANY",
        "uen_status_desc": "REGISTERED",
        "uen_issue_date": "2018-03-04",
    }
    base.update(over)
    return base


def test_live_local_company_is_mapped():
    row = acr._normalize(_rec())
    assert row is not None
    assert row["uen"] == "201812345A"
    assert row["company_name"] == "ACME PTE. LTD."
    assert row["entity_type"] == "LOCAL COMPANY"
    assert row["registration_date"] == "2018-03-04"
    assert row["country"] == "Singapore"
    assert row["source"] == "acra"


def test_missing_uen_or_name_is_skipped():
    assert acr._normalize(_rec(uen="")) is None
    assert acr._normalize(_rec(entity_name="")) is None


def test_non_accepted_entity_type_is_skipped():
    assert acr._normalize(_rec(entity_type_desc="GOVERNMENT AGENCY")) is None


def test_ceased_registration_is_skipped():
    """Only live/active registrations seed the table — a struck-off entity must
    never be presented as a registry match."""
    assert acr._normalize(_rec(uen_status_desc="CEASED REGISTRATION")) is None


def test_accepted_types_and_status_tokens_are_recognized():
    for t in acr.ACCEPTED_ENTITY_TYPES:
        assert acr._normalize(_rec(entity_type_desc=t)) is not None
    for s in acr.LIVE_STATUS_TOKENS:
        assert acr._normalize(_rec(uen_status_desc=s)) is not None


def test_alternate_field_names_are_supported():
    """The dataset field names drift; the normalizer accepts documented variants."""
    rec = {
        "uen": "53312345B",
        "company_name": "BETA LLP",           # variant of entity_name
        "entity_type": "LIMITED LIABILITY PARTNERSHIP",  # variant of entity_type_desc
        "status": "LIVE",                       # variant of uen_status_desc
        "incorporation_date": "2020-01-01",     # variant of uen_issue_date
    }
    row = acr._normalize(rec)
    assert row is not None
    assert row["company_name"] == "BETA LLP"
    assert row["registration_date"] == "2020-01-01"
