"""Regression test for the FSM-N05/FSM-N06 licence-scoping bug.

Before this fix: fsm_n05 and fsm_n06 were registered with
applies_to=_ALL_LICENCES, so every non-bank customer (MPI, SPI, insurer,
merchant bank, capital markets) received a Cyber Hygiene Attestation and
Governance Policy citing the bank Notice. Confirmed on a real Pro Suite
purchase for matchmove (mpi, licence class unconfirmed on the account) —
the delivery email cited "MAS Notice FSM-N06" while withholding the
Outsourcing Register for the same unconfirmed-licence reason.

Run: pytest tests/test_mas_notice_licence_scoping.py -v
"""
import pytest

from app.services import mas_notice_registry as registry
from app.services import trm_doc_generator as tdg


def test_fsm_n06_is_bank_only():
    assert registry.get("fsm_n06").applies_to == frozenset({"bank"})


def test_fsm_n05_is_bank_only():
    assert registry.get("fsm_n05").applies_to == frozenset({"bank"})


@pytest.mark.parametrize(
    "licence,expected_trm,expected_cyber_hygiene",
    [
        ("bank", "FSM-N05", "FSM-N06"),
        ("merchant_bank", "FSM-N11", "FSM-N12"),
        ("insurer", "FSM-N03", "FSM-N04"),
        ("capital_markets", "FSM-N21", "FSM-N22"),
    ],
)
def test_licence_resolves_to_correct_notice_pair(licence, expected_trm, expected_cyber_hygiene):
    trm = registry.technology_risk_instrument(licence)
    ch = registry.cyber_hygiene_instrument(licence)
    assert trm.ids[0] == expected_trm
    assert ch.ids[0] == expected_cyber_hygiene


@pytest.mark.parametrize("licence", ["mpi", "spi"])
def test_payment_licensees_resolve_cyber_hygiene_but_not_technology_risk(licence):
    """The pair is split for payment licensees, on purpose.

    FSM-N14 (cyber hygiene) does cover them — MAS's own cancellation page for
    Notice PSN06 redirects payment licensees to it. FSM-N13 (technology risk)
    does NOT follow the same scope: it binds operators and settlement
    institutions of *designated payment systems* and holders of a payment
    services licence for *digital payment token service*. Mapping every MPI/SPI
    to FSM-N13 would assert a binding Notice on entities it may not bind, so the
    technology-risk half raises and the three documents that depend on it gate.
    """
    assert registry.cyber_hygiene_instrument(licence).ids[0] == "FSM-N14"
    with pytest.raises(ValueError):
        registry.technology_risk_instrument(licence)


@pytest.mark.parametrize("licence", ["captive_insurer", "insurance_agent"])
def test_captives_and_agents_get_cyber_hygiene_but_not_technology_risk(licence):
    """Same split as mpi/spi, from MAS's own "Applies to" lists: FSM-N04 names
    Captive Insurer and General Insurance Agents; FSM-N03 names neither. Under
    the old single `insurer` key both were handed a document asserting FSM-N03
    bound them."""
    assert registry.cyber_hygiene_instrument(licence).ids[0] == "FSM-N04"
    with pytest.raises(ValueError):
        registry.technology_risk_instrument(licence)


def test_marine_mutual_insurers_resolve_neither_notice():
    """They appear in neither Notice's "Applies to". The key exists so the gate
    can fire — without it they normalise to `insurer` and silently collect both
    Notices that do not bind them."""
    for fn in (registry.technology_risk_instrument, registry.cyber_hygiene_instrument):
        with pytest.raises(ValueError):
            fn("marine_mutual_insurer")


def test_dtsp_resolves_fsm_n31_and_gates_technology_risk():
    """FSM-N31 is the DTSP cyber-hygiene Notice under the FSM Act 2022. No
    FSM-series TRM Notice was found for DTSPs, and FSM-N13 addresses the
    Payment Services Act digital-payment-token licence — a different regime."""
    assert registry.cyber_hygiene_instrument("dtsp").ids[0] == "FSM-N31"
    with pytest.raises(ValueError):
        registry.technology_risk_instrument("dtsp")


def test_bare_insurance_no_longer_normalises_to_a_licence():
    """"insurance" could mean a direct insurer, a captive, a marine mutual or an
    agent, and those are three different Notice sets. It must read as unknown."""
    from app.services.mas_licence import normalise_licence

    assert normalise_licence("insurance") is None
    assert normalise_licence("captive insurer") == "captive_insurer"
    assert normalise_licence("general insurance agent") == "insurance_agent"


def test_unknown_licence_never_guesses():
    with pytest.raises(ValueError):
        registry.technology_risk_instrument(None)
    with pytest.raises(ValueError):
        registry.cyber_hygiene_instrument(None)


def test_unmapped_licence_never_guesses():
    """other_fi has no single correct Notice pair — must raise, not default."""
    with pytest.raises(ValueError):
        registry.technology_risk_instrument("other_fi")
    with pytest.raises(ValueError):
        registry.cyber_hygiene_instrument("other_fi")


def test_matchmove_regression():
    """The exact case caught on the real test purchase: an mpi account must
    never see FSM-N06 (bank) anywhere in its Cyber Hygiene Attestation prompt,
    and must see FSM-N14 instead."""
    inst = registry.cyber_hygiene_instrument("mpi")
    ctx = tdg.TrmDocContext(
        legal_name="MATCHMOVE PAY PTE. LTD.", uen="200902936W",
        licence_type="mpi", sector="fintech", plan_label="Pro Suite",
    )
    prompt = tdg._cyber_hygiene_prompt(ctx, inst, strict=False)
    assert "FSM-N14" in prompt
    assert "FSM-N06" not in prompt


def test_bank_cyber_hygiene_prompt_still_cites_notice_655_cancellation():
    """Notice 655 (cancelled) was bank-specific — only the bank prompt should
    reference it. Non-bank licences should not, since their own predecessor
    notice numbers have not been verified (see mas_notice_registry.py)."""
    bank_inst = registry.cyber_hygiene_instrument("bank")
    bank_ctx = tdg.TrmDocContext(
        legal_name="DEMO BANK PTE. LTD.", uen="000000000X",
        licence_type="bank", sector="fintech", plan_label="Pro Suite",
    )
    bank_prompt = tdg._cyber_hygiene_prompt(bank_ctx, bank_inst, strict=False)
    assert "655" in bank_prompt

    mpi_inst = registry.cyber_hygiene_instrument("mpi")
    mpi_ctx = tdg.TrmDocContext(
        legal_name="MATCHMOVE PAY PTE. LTD.", uen="200902936W",
        licence_type="mpi", sector="fintech", plan_label="Pro Suite",
    )
    mpi_prompt = tdg._cyber_hygiene_prompt(mpi_ctx, mpi_inst, strict=False)
    assert "655" not in mpi_prompt


def test_licence_dependent_docs_are_gated_when_licence_unknown():
    """These five documents cite an entity-specific Notice and must now be
    withheld (not defaulted to the bank Notice) until the licence is
    confirmed — same discipline the Outsourcing Register already had."""
    gated_doc_types = {
        "trm_governance_policy",
        "cyber_hygiene_attestation",
        "outsourcing_risk_register",
        "incident_response_plan",
        "bcm_dr_plan",
        "access_control_policy",
    }
    actually_gated = {
        s.doc_type for s in tdg.TRM_DOCUMENT_CATALOG
        if s.requires_instrument is not None
    }
    assert actually_gated == gated_doc_types

    expected_when_unknown = tdg.expected_doc_types(None)
    assert expected_when_unknown == ("it_audit_vapt_log",)


def test_payment_licensee_receives_the_resolvable_subset_not_all_or_nothing():
    """Gating is per document. An MPI resolves FSM-N14 and the non-bank
    outsourcing Guidelines, so withholding its whole pack would be as wrong as
    stamping it with the bank Notice."""
    assert set(tdg.expected_doc_types("mpi")) == {
        "it_audit_vapt_log",
        "outsourcing_risk_register",
        "cyber_hygiene_attestation",
        "access_control_policy",
    }


def test_acceptance_matrix_expectations_match_the_generator():
    """The admin demo matrix hardcodes an `expect_docs` per case, and that number
    is asserted by a live-AI run nobody does on every change. Pin it against the
    generator here so a licence-mapping change surfaces in a two-second test
    rather than in a demo in front of a customer.

    The counts are deliberately unequal: the split of `insurer` means a captive
    insurer and a DTSP get 4 of 7 and a marine mutual gets 2, and those numbers
    are the visible consequence of MAS listing them where it does.
    """
    from app.services.trm_demo_harness import ACCEPTANCE_CASES

    for case in ACCEPTANCE_CASES:
        licence = case["mas_licence_type"]
        assert len(tdg.expected_doc_types(licence)) == case["expect_docs"], (
            f"case {case['case']} ({licence}) expects {case['expect_docs']} "
            f"documents, generator resolves {len(tdg.expected_doc_types(licence))}"
        )


def test_acceptance_matrix_covers_every_gating_outcome():
    """Case D (a direct insurer) passed both before and after the licence split,
    because its mapping never changed — a matrix of A-D could not have caught the
    captive-insurer defect. Every distinct outcome must have a case."""
    from app.services.trm_demo_harness import ACCEPTANCE_CASES

    covered = {len(tdg.expected_doc_types(c["mas_licence_type"])) for c in ACCEPTANCE_CASES}
    # 7 = fully mapped, 4 = cyber hygiene only, 2 = neither Notice, 1 = licence unset.
    assert covered == {7, 4, 2, 1}
