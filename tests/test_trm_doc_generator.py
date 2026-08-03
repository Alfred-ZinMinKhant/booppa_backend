"""MAS TRM Document Pack — Phase 7 acceptance tests.

Offline and deterministic by construction: `_call_text` / `_call_json` are
stubbed, so CI needs no `DEEPSEEK_API_KEY`, no broker, no S3 and no chain.

The stubs **echo the prompt back as the document body**. That is deliberate, not
laziness: everything these tests assert about regulatory correctness — which
outsourcing instrument was selected, which clause anchors were demanded, whether
the cancelled Notice 655 was handed over qualified — is decided caller-side
before the model is ever called. Asserting on a canned model reply would test the
fixture; asserting on the prompt tests the branch.
"""
from __future__ import annotations

import json
from datetime import date
from unittest.mock import patch

import pytest

from app.services import mas_notice_registry as registry
from app.services import trm_doc_generator as gen
from app.services import trm_pack_delivery as delivery
from app.services.mas_licence import MAS_LICENCE_TYPES, outsourcing_regime

BANK_KEYS = ("bank", "merchant_bank")
NON_BANK_KEYS = tuple(k for k in MAS_LICENCE_TYPES if k not in BANK_KEYS)


# ── fixtures ─────────────────────────────────────────────────────────────────

def _ctx(licence, *, gap: str | None = None) -> gen.TrmDocContext:
    return gen.TrmDocContext(
        legal_name="Meridian Bank (Singapore) Pte Ltd",
        uen="201812345K",
        licence_type=licence,
        sector="fintech",
        plan_label="Pro Suite",
        controls=tuple(
            gen.ControlSnapshot(
                domain=f"Domain {i}",
                control_ref=f"TRM-{i}",
                status="gap",
                risk_rating="high",
                gap_analysis=gap,
                evidence_count=2,
                tested_count=1,
                latest_tested_at="15 Mar 2026",
            )
            for i in range(1, 14)
        ),
    )


def _fsm_n06_body(prompt: str) -> str:
    """A body that satisfies `_validate_fsm_n06` while still carrying the prompt.

    Without the clause headings the attestation generator retries and then
    raises, so a naive echo stub would make every test look like a validator
    failure rather than exercising the path under test.
    """
    anchors = "\n".join(f"| {a} | ... | ... | ... | NOT IMPLEMENTED |"
                        for a in registry.get("fsm_n06").clause_anchors)
    return (
        "FSM-N06 attestation. FSM-N06 is binding. This addresses FSM-N06 "
        "clause by clause. Notice 655 was cancelled on 10 May 2024 and "
        "replaced.\n" + anchors + "\n\n=== PROMPT ECHO ===\n" + prompt
    )


@pytest.fixture
def stub_model():
    """Stub both model calls; expose every prompt sent for assertions."""
    prompts: list[str] = []

    def _text(system, user):
        prompts.append(user)
        if "Cyber Hygiene Compliance Attestation" in user:
            return _fsm_n06_body(user), 100, 200
        return f"## Document\n\n{user}", 100, 200

    def _json(system, user):
        prompts.append(user)
        return (
            {
                "header": f"Header. PROMPT ECHO: {user}",
                "columns": [],
                "rows": [{"Arrangement Ref": gen.UNEVIDENCED}],
                "notes": ["Reviewed annually."],
            },
            100,
            200,
        )

    with patch.object(gen, "_call_text", _text), patch.object(gen, "_call_json", _json):
        yield prompts


def _governing_section(prompt: str) -> str:
    """The `=== GOVERNING OUTSOURCING INSTRUMENT ===` block of a register prompt.

    Negative assertions must be scoped to this block, not the whole prompt: the
    prompt deliberately *names* the other regime in order to forbid it
    ("Do not cite MAS Notice 658 …"), so a whole-prompt `not in` would fail on
    exactly the sentence that makes the branch safe.
    """
    head = prompt.split("=== GOVERNING OUTSOURCING INSTRUMENT ===", 1)[1]
    return head.split("The governing instrument is", 1)[0]


# ── 1. the licence branch, over every licence key ────────────────────────────

@pytest.mark.parametrize("licence", sorted(MAS_LICENCE_TYPES))
def test_register_branches_on_licence(stub_model, licence):
    data, _, _ = gen.gen_outsourcing_register(_ctx(licence))
    cited = _governing_section(stub_model[-1])

    if licence == "bank":
        assert data["instrument"] == "MAS Notice 658 — Outsourcing (Banks)"
        assert "MAS Notice 658" in cited
        assert "1121" not in cited
        assert "Guidelines on Outsourcing" not in cited
    elif licence == "merchant_bank":
        assert data["instrument"] == "MAS Notice 1121 — Outsourcing (Merchant Banks)"
        assert "MAS Notice 1121" in cited
        assert "658" not in cited
        assert "Guidelines on Outsourcing" not in cited
    else:
        assert data["instrument"] == (
            "MAS Guidelines on Outsourcing (Financial Institutions other than Banks)"
        )
        assert "Guidelines on Outsourcing" in cited
        assert "658" not in cited
        assert "1121" not in cited


@pytest.mark.parametrize("licence", sorted(MAS_LICENCE_TYPES))
def test_register_prompt_forbids_the_other_regime_by_name(stub_model, licence):
    """Selecting the right instrument is half of it — the prompt must also close
    the door on the model volunteering the other one from training data."""
    gen.gen_outsourcing_register(_ctx(licence))
    prompt = stub_model[-1]

    if licence in BANK_KEYS:
        assert "Do not mention the Guidelines on Outsourcing" in prompt
    else:
        assert "Do not cite MAS Notice 658 or MAS Notice 1121" in prompt


@pytest.mark.parametrize("licence", BANK_KEYS)
def test_bank_register_uses_the_mas_specified_column_set(stub_model, licence):
    data, _, _ = gen.gen_outsourcing_register(_ctx(licence))
    # Columns are pinned caller-side; a register whose columns drifted to the
    # model's preference is not the instrument's specified format.
    assert data["columns"] == gen._BANK_REGISTER_COLUMNS
    assert data["regime"] == "bank_notices"


@pytest.mark.parametrize("licence", NON_BANK_KEYS)
def test_non_bank_register_uses_the_non_bank_column_set(stub_model, licence):
    data, _, _ = gen.gen_outsourcing_register(_ctx(licence))
    assert data["columns"] == gen._NON_BANK_REGISTER_COLUMNS
    assert data["regime"] == "non_bank_guidelines"


# ── 2. the Cyber Hygiene Attestation cites FSM-N06 specifically ──────────────

def test_attestation_cites_fsm_n06_not_generic_cyber_security(stub_model):
    content, _, _ = gen.gen_cyber_hygiene_attestation(_ctx("mpi"))

    assert content.count("FSM-N06") >= gen._FSM_N06_MIN_MENTIONS
    for anchor in registry.get("fsm_n06").clause_anchors:
        assert anchor.lower() in content.lower(), f"missing clause anchor: {anchor}"
    assert not gen._validate_fsm_n06(content)


def test_notice_655_never_appears_unqualified(stub_model):
    """655 is cancelled. It may appear only next to its cancellation."""
    gen.gen_cyber_hygiene_attestation(_ctx("mpi"))
    prompt = next(p for p in stub_model if "Cyber Hygiene Compliance Attestation" in p)

    lowered = prompt.lower()
    idx = lowered.find("notice 655")
    assert idx != -1, "655 must be handed over explicitly as superseded, not omitted"
    while idx != -1:
        window = lowered[max(0, idx - 200): idx + 200]
        assert any(w in window for w in ("cancel", "supersed", "replaced", "no longer")), \
            "Notice 655 cited without qualification"
        idx = lowered.find("notice 655", idx + 1)


def test_validator_rejects_a_generic_cyber_security_document():
    generic = "We take cyber security seriously. " * 50
    problems = gen._validate_fsm_n06(generic)
    assert problems
    assert any("FSM-N06" in p for p in problems)


# ── 3. unknown licence gates rather than defaults ────────────────────────────

def test_outsourcing_regime_raises_on_unknown_licence():
    with pytest.raises(ValueError):
        outsourcing_regime(None)
    with pytest.raises(ValueError):
        outsourcing_regime("not-a-real-licence")


def test_unknown_licence_skips_the_register_and_emits_no_default_regime(stub_model):
    results = gen.generate_all_trm_documents(_ctx(None))
    by_type = {r["doc_type"]: r for r in results}

    register = by_type["outsourcing_risk_register"]
    assert register["skipped"] is True
    assert register["skip_reason"] == "mas_licence_type_not_confirmed"
    assert register["content"] is None

    # No prompt may name an outsourcing instrument when the licence is unknown.
    joined = "\n".join(stub_model)
    assert "Notice 658" not in joined
    assert "Notice 1121" not in joined
    assert "Guidelines on Outsourcing" not in joined

    delivered = [r for r in results if not r.get("skipped")]
    assert len(delivered) == 6


def test_unknown_licence_is_blocked_not_error(stub_model):
    docs = {
        dt: {"url": "https://s3.example/x.pdf"}
        for dt in gen.expected_doc_types(None)
    }
    status, missing = delivery.evaluate_pack_gate(docs, None)
    assert status == delivery.PACK_STATUS_BLOCKED
    assert missing == []


# ── 4. the completeness gate ─────────────────────────────────────────────────

def test_gate_fails_when_any_expected_document_is_missing():
    docs = {
        dt: {"url": "https://s3.example/x.pdf"}
        for dt in gen.expected_doc_types("mpi")
    }
    docs["incident_response_plan"].pop("url")

    status, missing = delivery.evaluate_pack_gate(docs, "mpi")
    assert status == delivery.PACK_STATUS_ERROR
    assert missing == ["incident_response_plan"]


def test_gate_counts_generated_but_undelivered_as_missing():
    """The CSP failure mode: generated content with no download link is not a
    delivered document, and `generated_ok > 0` is not a gate."""
    docs = {
        dt: {"url": "https://s3.example/x.pdf"}
        for dt in gen.expected_doc_types("bank")
    }
    docs["bcm_dr_plan"] = {"content": "a full plan", "error": "upload failed"}

    status, missing = delivery.evaluate_pack_gate(docs, "bank")
    assert status == delivery.PACK_STATUS_ERROR
    assert "bcm_dr_plan" in missing


def test_gate_scopes_to_the_requested_subset_on_a_licence_correction():
    """The correction path regenerates the register alone. Without subset
    scoping the gate reports the six it never attempted as missing, fails the
    run, and withholds the one document the customer is waiting for."""
    docs = {"outsourcing_risk_register": {"url": "https://s3.example/r.pdf"}}

    status, missing = delivery.evaluate_pack_gate(
        docs, "bank", only_doc_types=["outsourcing_risk_register"]
    )
    assert status == delivery.PACK_STATUS_READY
    assert missing == []


# ── 5. cost invariants ───────────────────────────────────────────────────────

def test_ctx_block_stays_under_the_cap_with_maximal_gap_narratives():
    ctx = _ctx("bank", gap="X" * 50_000)
    block = gen._ctx_block(ctx)
    assert len(block) <= gen._CTX_MAX_CHARS + len("\n[context truncated]")


def test_per_control_gap_is_truncated_in_the_prompt():
    ctx = _ctx("bank", gap="Y" * 10_000)
    block = gen._ctx_block(ctx)
    assert "Y" * (gen._GAP_MAX_CHARS + 1) not in block


def test_assessment_data_is_never_passed_to_a_model(stub_model):
    """Report.assessment_data is a large JSONB blob. It is not a prompt input,
    and the context is built from control rows precisely so it cannot become one."""
    gen.generate_all_trm_documents(_ctx("bank"))
    joined = "\n".join(stub_model)
    assert "assessment_data" not in joined
    assert "booppa_report" not in joined


# ── 6. registry integrity ────────────────────────────────────────────────────

def test_every_applies_to_key_is_a_real_licence_key():
    for inst in registry.MAS_INSTRUMENTS.values():
        unknown = set(inst.applies_to) - set(MAS_LICENCE_TYPES)
        assert not unknown, f"{inst.key} applies_to unknown licence(s): {unknown}"


def test_notice_655_is_recorded_as_superseded():
    inst = registry.get("notice_655")
    assert inst.status == "superseded"
    assert inst.is_current is False
    assert inst.binding is False
    assert inst.superseded_on == date(2024, 5, 10)
    assert inst.superseded_by == "fsm_n06"


def test_every_catalog_notice_key_resolves():
    for spec in gen.TRM_DOCUMENT_CATALOG:
        for key in spec.notices:
            assert registry.get(key).key == key


def test_citation_block_never_offers_a_superseded_instrument_as_current():
    block = registry.citation_block(["fsm_n06", "notice_655"])
    assert "DO NOT CITE AS CURRENT" in block
    assert "cancelled on 10 May 2024" in block


# ── 7. the PDF carries the draft banner and signature block ──────────────────

def test_every_rendered_pdf_carries_the_draft_banner(stub_model):
    """These are drafts until the entity signs them. A Cyber Hygiene
    *Attestation* is a representation to MAS — an unbannered PDF is the whole
    legal exposure of this product."""
    from pypdf import PdfReader
    from io import BytesIO

    ctx = _ctx("bank")
    results = gen.generate_all_trm_documents(ctx)
    assert len(results) == 7

    for result in results:
        pdf_bytes, sha = delivery.render_pack_document(result, ctx)
        assert pdf_bytes and len(sha) == 64

        text = "".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf_bytes)).pages)
        collapsed = " ".join(text.split()).upper()
        assert "AI-GENERATED DRAFT" in collapsed, result["doc_type"]
        # Unsigned, it carries no evidentiary value; the block is where that
        # gets fixed, so its absence would make the draft banner unactionable.
        assert "APPROVAL & SIGNATURE" in collapsed, result["doc_type"]
        assert "SIGNATURE" in collapsed, result["doc_type"]


# ── 8. catalog parity ────────────────────────────────────────────────────────

def test_catalog_and_gate_agree_on_what_a_complete_pack_is():
    assert set(gen.expected_doc_types("bank")) == set(gen.TRM_DOC_TYPES)
    assert len(gen.TRM_DOC_TYPES) == 7
    assert set(gen.expected_doc_types(None)) == set(gen.TRM_DOC_TYPES) - {
        "outsourcing_risk_register"
    }


def test_every_generator_takes_exactly_one_argument():
    """The CSP bug was arity introspection over `co_varnames`, which counts
    locals. This catalog is immune because every generator is `fn(ctx)` — that
    property has to be enforced, not just documented."""
    for spec in gen.TRM_DOCUMENT_CATALOG:
        assert spec.fn.__code__.co_argcount == 1, spec.doc_type


# ── 9. CSP regression — the Phase 0 bug must stay fixed ──────────────────────

def test_all_csp_documents_return_non_none_content():
    """`co_varnames` counted locals, so the five `needs_clients=False`
    generators took the two-arg branch, raised TypeError, and were emitted with
    `content: None`. Paying customers received 3-of-8 packs."""
    from app.services import csp_doc_generator as csp

    profile = {
        "legal_name": "NovaPay Fintech Pte Ltd",
        "uen": "201812345K",
        "entity_type": "Private Limited Company",
        "registered_address": "1 Raffles Place, Singapore",
    }
    with patch.object(csp, "_call", lambda system, user: ("## Doc\n\nbody", 10, 20)):
        results = csp.generate_all_csp_documents(profile, clients=[])

    assert len(results) == 8
    for r in results:
        assert r.get("content") is not None, (
            f"{r['doc_type']} generated None: {r.get('error')}"
        )


def test_csp_profile_only_generators_are_dispatched_by_argcount():
    """`len(co_varnames)` counts locals; `co_argcount` does not. Every
    profile-only generator declares locals, which is why the old check sent all
    five down the two-argument branch."""
    from app.services import csp_doc_generator as csp

    for _dt, _title, fn, needs_clients in csp.CSP_DOCUMENT_CATALOG:
        expected = 2 if needs_clients else 1
        assert fn.__code__.co_argcount == expected, fn.__name__
        if not needs_clients:
            assert len(fn.__code__.co_varnames) > 1, (
                f"{fn.__name__} declares no locals — it would not have caught the bug"
            )
