"""RFP kit intake-fill + quality gate (Phase D of the forensic-audit remediation).

The AI leaves `[Verify: …]` placeholders for facts it can't ground from the
website. When the buyer supplied that fact in the intake, the kit must use it
instead of shipping the placeholder; facts the buyer never gave stay as
placeholders for them to complete, and a near-empty kit is counted so the
fulfillment flow can alert.
"""
from app.services.rfp_express_builder import RFPExpressBuilder


def _builder():
    b = RFPExpressBuilder.__new__(RFPExpressBuilder)
    b.warnings = []
    b.used_template = False
    return b


def test_intake_facts_replace_verify_placeholders():
    b = _builder()
    intake = {
        "iso_cert_number": "SG-ISO-12345",
        "iso_cert_expiry": "2027-03-01",
        "bcp_last_tested": "Feb 2026",
        "key_processors": "Stripe, AWS",
        "training_frequency": "quarterly",
    }
    qa = {
        "data_protection": "We hold [Verify: ISO 27001 cert number and expiry].",
        "incident_response": "BCP is [Verify: BCP last test date].",
        "subcontracting": "Our sub-processors: [Verify: sub-processors].",
        "audit_logging": "Training runs [Verify: cadence].",
        "encryption_standards": "We use [Verify: encryption standard] at rest.",
    }
    out = b._apply_intake_substitutions(qa, intake)

    assert "SG-ISO-12345" in out["data_protection"]
    assert "2027-03-01" in out["data_protection"]
    assert "[Verify:" not in out["data_protection"]
    assert "Feb 2026" in out["incident_response"]
    assert "Stripe, AWS" in out["subcontracting"]
    assert "quarterly" in out["audit_logging"]
    # No intake field for encryption → placeholder is correctly left for the buyer.
    assert "[Verify: encryption standard]" in out["encryption_standards"]


def test_no_intake_leaves_answers_untouched():
    b = _builder()
    qa = {"q": "We use [Verify: encryption standard]."}
    assert b._apply_intake_substitutions(qa, {}) == qa
    assert b._apply_intake_substitutions(qa, None) == qa


def test_residual_placeholder_count():
    b = _builder()
    qa = {
        "a": "Clean answer, no placeholders.",
        "b": "Needs [Verify: SLA target] and ___ [FILL IN] ___ here.",
        "c": "Another [Verify: retention period].",
    }
    # [Verify: SLA target], [FILL IN], [Verify: retention period] = 3
    assert b._count_residual_placeholders(qa) == 3
    assert b._count_residual_placeholders({"x": "all good"}) == 0


def test_residual_details_carry_instructional_guidance():
    """Surviving placeholders shown to the blocked buyer must include a
    where-to-find + format hint, not just the bare marker (Sprint 5a)."""
    b = _builder()
    qa = {
        "iso": "We hold [Verify: ISO 27001 cert number].",
        "enc": "Data is [Verify: encryption standard].",
        "dpo": "Our [Verify: DPO name and email].",
    }
    details = b._residual_placeholder_details(qa)
    joined = "\n".join(details)
    # Each marker is preserved AND enriched with guidance.
    assert any(d.startswith("[Verify: ISO 27001 cert number]") and "—" in d for d in details)
    assert "ISO/IEC 27001" in joined          # ISO format hint
    assert "Security Hub" in joined           # encryption where-to-find hint
    assert "Data Protection Officer" in joined  # DPO hint
    # Distinct markers only.
    assert len(details) == 3
