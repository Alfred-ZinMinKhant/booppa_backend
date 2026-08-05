"""Regulatory citation registry — structure, staleness, and the two known errors.

Everything in the registry is printed into signed, hash-anchored customer
documents. These tests exist so that the two citation errors we actually shipped
cannot come back: FSM-N05/N06 cited for every MAS licence class, and tipping-off
cited as CDSA s.48A -- then, on the "correction", as s.48.
"""

from datetime import date, timedelta

import pytest

from app.services import mas_notice_registry as mas
from app.services import regulatory_registry as reg

# Instruments knowingly carried without a primary-source check. This list is the
# gate: adding a new instrument without verifying it fails CI until someone
# either checks it against the issuing body's page and sets `verified_on`, or
# consciously adds it here. It must only ever shrink.
PENDING_VERIFICATION: set = set()
# Empty, and it must stay that way: every instrument in every table has been
# read against the issuing body's own page and carries a `source_url`. A new
# row therefore has to arrive verified, or be added here deliberately with a
# reason -- which is the point. This set must only ever shrink.
#
# It has held two backlogs. The first was the fourteen non-MAS rows encoded
# from generator prose (five wrong). The second was the sixteen MAS rows whose
# verified_on predated `source_url`, so there was no record of what had been
# read -- also five wrong once someone read them. Both were emptied on
# 2026-08-05. The hit rate is the argument for the gate.


def test_every_body_is_registered():
    assert set(reg.bodies()) == {
        reg.BODY_MAS, reg.BODY_PDPC, reg.BODY_ACRA, reg.BODY_CDSA,
        reg.BODY_IMDA, reg.BODY_GOVTECH, reg.BODY_MOF, reg.BODY_SAC,
        reg.BODY_FATF,
    }, "MAS registers itself lazily on first lookup — see _load_all"


def test_every_verified_row_says_what_it_was_verified_against():
    """A date with no URL is the unfalsifiable assurance the per-instrument
    `verified_on` was introduced to replace. `register_body` rejects it at
    import; this pins the invariant where a reader will find it."""
    missing = [
        i.ref for i in reg.all_instruments()
        if i.verified_on is not None and not i.source_url
    ]
    assert not missing, f"verified with no source_url: {missing}"


def test_spam_control_act_is_imda_not_pdpc():
    """It sat under PDPC because unsolicited marketing engages both it and the
    PDPA's DNC provisions — and the DNC Registry really is PDPC's."""
    inst = reg.get(reg.BODY_IMDA, "spam_control_act")
    assert inst.issuing_body == reg.BODY_IMDA
    with pytest.raises(KeyError):
        reg.get(reg.BODY_PDPC, "spam_control_act")


def test_breach_notification_is_part_6a_not_6b():
    """Part 6B is Data Portability; the adjacent Part is easy to mis-copy and
    the section range (26A-26E) was right throughout, which hid it."""
    inst = reg.get(reg.BODY_PDPC, "pdpa_breach_notification")
    assert "Part 6A" in inst.citation
    assert "6B" not in inst.citation and "6B" not in " ".join(inst.ids)


def test_gebiz_access_and_supplier_registration_are_separate_schemes():
    """Conflating them either invents a barrier to bidding (GSR is not needed
    to quote) or loses a bid (GTP does not satisfy a GSR-gated tender)."""
    gtp = reg.get(reg.BODY_GOVTECH, "gebiz_registration")
    gsr = reg.get(reg.BODY_MOF, "supplier_registration")
    assert "Trading Partner" in gtp.citation
    assert "Government Supplier Registration" in gsr.citation
    # The invented term that merged them.
    for inst in (gtp, gsr):
        assert "supply category" not in " ".join(inst.key_obligations).lower()


def test_unverified_set_is_exactly_the_declared_backlog():
    """A new instrument must arrive either verified or explicitly acknowledged."""
    actual = {i.ref for i in reg.all_instruments() if i.verified_on is None}
    assert actual == PENDING_VERIFICATION, (
        "verified_on backlog changed — set verified_on after checking the "
        "issuing body's own page, or update PENDING_VERIFICATION deliberately"
    )


def test_no_verified_instrument_is_past_its_re_verification_window():
    stale = [
        (i.ref, why) for i, why in reg.stale_instruments()
        if i.verified_on is not None
    ]
    assert not stale, f"instruments past re-verification: {stale}"


def test_stale_detection_reports_never_verified(monkeypatch):
    """The never-verified branch needs an unverified row, and there is no longer
    a real one — PENDING_VERIFICATION is empty. Register a throwaway body rather
    than leaving a genuine citation unverified just to keep a test honest."""
    body = "TEST-BODY"
    inst = reg.Instrument(
        key="unchecked", issuing_body=body, verified_on=None, ids=(),
        applies_to=frozenset(), title="Never verified",
        instrument_type="notice", binding=True, status="current",
        citation="A row nobody has read",
    )
    monkeypatch.setitem(reg._BODIES, body, {"unchecked": inst})

    stale = reg.stale_instruments(as_of=date(2026, 8, 5), max_age_days=100)
    hit = {i.ref: why for i, why in stale}
    assert hit.get(f"{body}:unchecked") == "never verified against the primary source"


def test_stale_detection_reports_overdue_but_not_freshly_verified():
    today = date(2026, 8, 5)
    refs = {i.ref for i, _ in reg.stale_instruments(as_of=today, max_age_days=100)}
    # verified today, well inside the window
    assert "CDSA:tipping_off" not in refs
    assert "MAS:fsm_n06" not in refs

    later = today + timedelta(days=200)
    refs_later = {i.ref for i, _ in reg.stale_instruments(as_of=later)}
    assert "CDSA:tipping_off" in refs_later
    assert "PDPC:pdpa" in refs_later
    assert "MAS:fsm_n06" in refs_later


def test_superseded_instruments_are_never_reported_as_stale():
    """Notice 655 is cited only *as cancelled*; it cannot go out of date."""
    refs = {i.ref for i, _ in reg.stale_instruments(as_of=date(2030, 1, 1))}
    assert "MAS:notice_655" not in refs


def test_tipping_off_is_cdsa_s57_not_s48_or_s48a():
    """Third value for this number. s.48A is cross-border cash reporting;
    s.48 is communication of information to a foreign authority; the offence
    is s.57 "Tipping-off" in Part 6."""
    inst = reg.get(reg.BODY_CDSA, "tipping_off")
    assert inst.ids[0] == "CDSA s.57"
    assert "48A" not in inst.citation
    assert "section 48 of" not in inst.citation
    assert "section 57 of the Corruption" in inst.citation


def test_notice_655_applied_to_banks_only():
    """It shipped as _ALL_LICENCES. MAS's page lists Full and Wholesale Banks
    only, so the wider set told other licensees they were once bound by it."""
    inst = mas.get("notice_655")
    assert inst.applies_to == frozenset({"bank"})


def test_outsourcing_notices_use_their_published_titles():
    """"Outsourcing (Banks)" was our paraphrase. A paraphrase printed in a
    signed attestation is not a citation."""
    for key, who in (("notice_658", "Banks"), ("notice_1121", "Merchant Banks")):
        inst = mas.get(key)
        assert inst.title == f"Management of Outsourced Relevant Services for {who}"
        assert "Management of Outsourced Relevant Services" in inst.citation


def test_trm_guidelines_use_the_published_title():
    inst = mas.get("trm_guidelines")
    assert inst.title == "Guidelines on Risk Management Practices — Technology Risk"


def test_insurer_notices_state_their_differing_scopes():
    """FSM-N03 lists six direct-insurer/reinsurer classes; FSM-N04 lists those
    six plus captives and general insurance agents. Both rows were labelled
    "(Insurers)" and shared one licence key, which told a captive insurer
    FSM-N03 binds it when MAS does not list it at all."""
    n03 = mas.get("fsm_n03")
    n04 = mas.get("fsm_n04")
    assert "not captive insurers" in n03.citation
    assert "captive insurers and general insurance agents" in n04.citation
    assert n03.citation != n04.citation
    # The scope difference is now enforced by applies_to, not only stated in
    # prose — prose cannot gate anything.
    assert n03.applies_to == frozenset({"insurer"})
    assert n04.applies_to == frozenset({"insurer", "captive_insurer", "insurance_agent"})
    assert n04.applies_to > n03.applies_to


def test_fsm_n31_is_registered_and_scoped_to_dtsps():
    """Found during the MAS pass and initially left as a docstring note. An
    instrument recorded in prose is not in the registry, and a row nobody can
    reach is not verified either — it now has a licence key that reaches it."""
    inst = mas.get("fsm_n31")
    assert inst.ids == ("FSM-N31",)
    assert inst.applies_to == frozenset({"dtsp"})
    assert inst.verified_on is not None and inst.source_url


def test_csp_generators_read_the_section_from_the_registry():
    """The guard message and the customer-facing prose must agree with the row."""
    from app.services import csp_baseline_generator as baseline
    from app.services import csp_doc_generator as docs

    expected = reg.get(reg.BODY_CDSA, "tipping_off").ids[0]
    assert baseline._TIPPING_OFF_SHORT == expected
    assert docs._TIPPING_OFF_SHORT == expected


def test_mas_facade_still_answers_the_old_api():
    """`mas_notice_registry` is now a data module over the generic registry;
    every caller and test still uses its original surface."""
    from app.services import mas_notice_registry as mas

    assert mas.get("fsm_n06").citation.startswith("MAS Notice FSM-N06")
    assert mas.cyber_hygiene_instrument("mpi").ids[0] == "FSM-N14"
    with pytest.raises(ValueError):
        mas.technology_risk_instrument("mpi")
    block = mas.citation_block(["notice_655", "fsm_n06"])
    assert "DO NOT CITE AS CURRENT" in block
    assert "cancelled on 10 May 2024" in block


def test_registry_lookup_raises_rather_than_returning_a_blank_citation():
    with pytest.raises(KeyError):
        reg.get(reg.BODY_PDPC, "pdpa_s99")
    with pytest.raises(KeyError):
        reg.get("NOT_A_BODY", "pdpa")


def test_register_body_rejects_a_row_filed_under_the_wrong_body():
    row = reg.Instrument(
        issuing_body=reg.BODY_PDPC, key="x", ids=(), title="x",
        instrument_type="act", binding=True, status="current",
        applies_to=frozenset(), citation="x",
    )
    with pytest.raises(ValueError):
        reg.register_body(reg.BODY_ACRA, {"x": row})
    with pytest.raises(ValueError):
        reg.register_body(reg.BODY_PDPC, {"y": row})
