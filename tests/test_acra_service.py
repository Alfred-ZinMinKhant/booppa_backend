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


# ---------------------------------------------------------------------------
# Targeted pass (`require_live=False`) — keeps ceased entities, records status.
#
# These pin the two invariants that make the targeted pass safe to run beside the
# monthly sweep: the sweep must never emit a registry status, and the targeted
# status must survive a later sweep upsert of the same UEN.
# ---------------------------------------------------------------------------

import asyncio
import json

import pytest


def test_targeted_keeps_ceased_record_that_sweep_drops():
    """The same record: dropped by the sweep, kept with its status by the targeted pass."""
    rec = _rec(uen_status_desc="Struck Off")

    assert acr._normalize(rec) is None

    row = acr._normalize(rec, require_live=False)
    assert row is not None
    assert row["registry_status"] == "Struck Off"
    assert row["registry_checked_at"] is not None
    assert row["uen"] == "201812345A"


def test_targeted_keeps_entity_type_outside_sweep_allowlist():
    rec = _rec(entity_type_desc="PUBLIC ACCOUNTING FIRM")
    assert acr._normalize(rec) is None
    assert acr._normalize(rec, require_live=False) is not None


def test_sweep_never_emits_registry_status():
    """The sweep only ever sees live rows, so any status it wrote would be an
    unearned positive claim — and would clobber a targeted 'Struck Off'."""
    row = acr._normalize(_rec())
    assert "registry_status" not in row
    assert "registry_checked_at" not in row


class _FakeResp:
    status_code = 200
    headers: dict = {}

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _FakeClient:
    """Datastore stub. `honours_list=False` mimics a server that ignores the
    list-valued `filters` and answers with a single row."""

    def __init__(self, records, honours_list=True):
        self._records = records
        self._honours_list = honours_list
        self.requests: list[dict] = []

    async def get(self, url, params=None, headers=None):
        self.requests.append(params)
        wanted = json.loads(params["filters"])["uen"]
        if isinstance(wanted, str):
            wanted = [wanted]
        hits = [r for r in self._records if r["uen"] in wanted]
        if not self._honours_list:
            hits = hits[:1]
        return _FakeResp({"result": {"records": hits}})


@pytest.fixture
def _no_delay(monkeypatch):
    monkeypatch.setattr(acr, "INTER_PAGE_DELAY", 0)


TARGET_RECORDS = [
    _rec(uen="200011111A", entity_name="CEASED CO", uen_status_desc="Struck Off"),
    _rec(uen="200022222B", entity_name="LIVE CO", uen_status_desc="Registered"),
]
TARGET_UENS = ["200011111A", "200022222B"]


def test_batch_probe_detects_working_list_filter(_no_delay):
    client = _FakeClient(TARGET_RECORDS, honours_list=True)
    records, batched = asyncio.run(acr._fetch_by_uens(client, "ds", TARGET_UENS))
    assert batched is True
    assert {r["uen"] for r in records} == set(TARGET_UENS)


def test_falls_back_to_per_uen_and_still_matches_every_uen(_no_delay):
    """A server that ignores the list form must not look like '1 match, N-1 not in
    ACRA' — the fallback has to recover full coverage."""
    client = _FakeClient(TARGET_RECORDS, honours_list=False)
    records, batched = asyncio.run(acr._fetch_by_uens(client, "ds", TARGET_UENS))
    assert batched is False
    assert {r["uen"] for r in records} == set(TARGET_UENS)
    # one probe + one request per UEN
    assert len(client.requests) == 1 + len(TARGET_UENS)


# ── Profile surfacing: registry facts ────────────────────────────────────────

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2018-03-04", 2018),
        ("04/03/2018", 2018),
        ("1985-01-01", 1985),
        (None, None),
        ("", None),
        ("n/a", None),
    ],
)
def test_registered_since_year_extracts_only_a_year(raw, expected):
    """The stored registration date is an unparsed registry string, so a year is
    the narrowest claim it can support — and an unparseable value must yield None
    rather than a guess."""
    assert acr.registered_since_year(raw) == expected


class _BoomSession:
    def query(self, *a, **kw):
        raise RuntimeError("registry table unavailable")


def test_registry_facts_absent_rather_than_raising_when_lookup_fails():
    """A profile page must still render when the registry lookup breaks. Absent
    facts are the honest fallback; a raise would take the whole page down over a
    supplementary field."""
    facts = acr.resolve_registry_facts(_BoomSession(), "200011111A")
    assert facts == {"registered_since": None, "registry_status": None}


def test_registry_facts_never_default_status_to_live():
    """NULL registry_status means 'never targeted-checked', NOT 'live'. Every
    unknown path must surface None so renderers omit the field."""
    assert acr.resolve_registry_facts(_BoomSession(), None)["registry_status"] is None
    assert acr.resolve_registry_facts_bulk(_BoomSession(), []) == {}


def _expire_after_first_call():
    """monotonic() stub that jumps far past any budget on every call.

    Deliberately not "0 first, then large": `acr.time` is the shared stdlib
    module and asyncio's event loop reads monotonic() as its own clock, so a
    stub keyed on call ordering hands its first value to the loop instead of to
    the deadline. Any two consecutive reads must exceed the budget.
    """
    calls = {"n": 0}

    def _mono():
        calls["n"] += 1
        return calls["n"] * 1000.0

    return _mono


# ── Resumability: a bounded pass must persist and continue ───────────────────


def test_deadline_stops_the_pass_but_keeps_what_it_wrote(_no_delay, monkeypatch):
    """The failure this guards against actually happened in production: the pass
    fetched everything before writing anything, ran out of time, and persisted
    zero rows after 250 seconds of load on data.gov.sg. Each chunk must be
    written as it lands, and the run must report what it did not reach."""
    uens = [f"20001{i:04d}A" for i in range(200)]
    records = [_rec(uen=u, entity_name=f"CO {u}", uen_status_desc="Registered")
               for u in uens]
    client = _FakeClient(records, honours_list=True)

    written: list[dict] = []
    monkeypatch.setattr(acr, "_upsert_targeted",
                        lambda db, rows: (written.extend(rows), len(rows))[1])
    # Budget expires after the first chunk. A counter rather than a fixed list:
    # `acr.time` is the shared stdlib module, so monotonic() is called more often
    # than just by the deadline check and an exhaustible iterator would blow up.
    monkeypatch.setattr(acr.time, "monotonic", _expire_after_first_call())

    result = asyncio.run(acr._refresh_targeted_async(None, "ds", uens, budget_seconds=1, client=client))

    assert result["complete"] is False
    assert result["remaining"] == len(uens) - result["processed"]
    assert result["processed"] > 0
    # The decisive assertion: work reached the database before the deadline hit.
    assert len(written) == result["processed"] > 0


def test_unmatched_counts_only_what_was_actually_processed(_no_delay, monkeypatch):
    """A UEN the pass never reached is not evidence of absence from the register.
    Counting unreached UENs as `unmatched` would overstate how many of our UENs
    ACRA cannot find — the number we use to judge the claim's trustworthiness."""
    uens = [f"20002{i:04d}B" for i in range(200)]
    client = _FakeClient([], honours_list=True)  # nothing matches
    monkeypatch.setattr(acr, "_upsert_targeted", lambda db, rows: len(rows))
    monkeypatch.setattr(acr.time, "monotonic", _expire_after_first_call())

    result = asyncio.run(acr._refresh_targeted_async(None, "ds", uens, budget_seconds=1, client=client))

    assert result["unmatched"] == result["processed"]
    assert result["unmatched"] < len(uens)


def test_resume_order_puts_never_checked_uens_first():
    """Successive bounded passes only converge on full coverage if each one starts
    where the last stopped. Never-checked UENs outrank a refresh of a known one."""
    from datetime import datetime

    class _Sess:
        def query(self, *a, **kw):
            return self

        def filter(self, *a, **kw):
            return self

        def all(self):
            return [("AAA", datetime(2026, 1, 1)), ("BBB", None)]

    ordered = acr._order_for_resume(_Sess(), ["AAA", "BBB", "CCC"])
    # BBB (row exists, never checked) and CCC (no row at all) both precede the
    # already-checked AAA.
    assert ordered.index("AAA") == 2
    assert set(ordered[:2]) == {"BBB", "CCC"}


def test_resume_order_falls_back_rather_than_blocking_the_backfill():
    class _Boom:
        def query(self, *a, **kw):
            raise RuntimeError("no such column")

    assert acr._order_for_resume(_Boom(), ["AAA", "BBB"]) == ["AAA", "BBB"]
