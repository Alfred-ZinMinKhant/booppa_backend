"""RFP builder invariants: idempotent report_id + placeholder-aware VERIFIED gate.

Two forensic findings drive these:

  * CB-3 (cross-order file reuse): `report_id = uuid5(NAMESPACE_URL, f"rfp:{session_id}")`
    is DETERMINISTIC on session_id. Two independent paid orders have different Stripe
    session_ids, so they must produce different report_ids (and thus different files).
    Byte-identical kits across two orders can only happen when the same session_id is
    reused — a test artifact, not a production path. These tests lock that contract.

  * RFP-2 (false VERIFIED badge): the badge must be gated on the answer text actually
    being substituted — an answer still carrying a `[Verify: …]` / `[FILL IN]`
    placeholder must never be badged VERIFIED, even if an evidence source touched the
    field. `_PLACEHOLDER_RE` is the substitution-completeness signal used in that gate.
"""
import uuid

from app.services.rfp_express_builder import RFPExpressBuilder


def _b(session_id):
    return RFPExpressBuilder(vendor_id="v@x.io", vendor_email="v@x.io", session_id=session_id)


def test_distinct_sessions_yield_distinct_report_ids():
    """Two independent orders (distinct Stripe session_ids) → distinct report_ids,
    so one order can never re-serve another order's anchored evidence."""
    a = _b("cs_test_ORDER_A").report_id
    b = _b("cs_test_ORDER_B").report_id
    assert a != b, "distinct sessions collided on report_id — cross-order reuse risk"


def test_same_session_is_idempotent():
    """Same session_id → identical report_id (safe Celery retries of ONE order)."""
    a = _b("cs_test_SAME").report_id
    a2 = _b("cs_test_SAME").report_id
    assert a == a2
    assert a == str(uuid.uuid5(uuid.NAMESPACE_URL, "rfp:cs_test_SAME"))


def test_no_session_falls_back_to_random_unique_id():
    """No session_id → a fresh uuid4 each time (never a shared deterministic id)."""
    a = _b(None).report_id
    b = _b(None).report_id
    assert a != b


# ── RFP-2: placeholder-aware VERIFIED gate ────────────────────────────────────

def test_placeholder_regex_matches_unfilled_markers():
    rx = RFPExpressBuilder._PLACEHOLDER_RE
    assert rx.search("[Verify: encryption standard for data at rest and in transit]")
    assert rx.search("Our DPO is [FILL IN].")


def test_placeholder_regex_ignores_completed_answers():
    rx = RFPExpressBuilder._PLACEHOLDER_RE
    assert not rx.search("AES-256 encryption at rest and TLS 1.3 in transit.")
    assert not rx.search("Data Protection Officer: Jane Tan, dpo@acme.sg")


def _verified(source: str, answer: str) -> bool:
    """Replicate the exact predicate the builder uses to badge an Appendix-D item
    (rfp_express_builder.generate_express_package): an evidence source alone is NOT
    enough — the visible answer must also be free of unfilled placeholders."""
    return source != "ai_drafted" and not RFPExpressBuilder._PLACEHOLDER_RE.search(answer or "")


def test_evidence_touch_alone_does_not_verify_an_unfilled_placeholder():
    """The precise defect: an SSL/evidence source touched the field, but the answer
    text is still the raw '[Verify: …]' placeholder → must NOT be VERIFIED."""
    assert _verified("ssl", "[Verify: encryption standard for data at rest and in transit]") is False


def test_real_substituted_answer_with_evidence_verifies():
    assert _verified("ssl", "TLS 1.3 in transit; AES-256 at rest.") is True


def test_ai_drafted_never_verifies():
    assert _verified("ai_drafted", "TLS 1.3 in transit; AES-256 at rest.") is False


# ── Privacy-policy URL must be the vendor's OWN, never a linked-out policy ─────
# Recurring bug: a vendor page embeds Google reCAPTCHA, which links to
# policies.google.com/privacy — that URL was being presented as the vendor's
# published privacy policy in the kit.

def test_same_site_rejects_foreign_privacy_host():
    from app.services.rfp_express_builder import _same_site
    assert _same_site("policies.google.com", "www.ensigninfosecurity.com") is False
    assert _same_site("cdn.cloudflare.com", "acme.sg") is False


def test_same_site_accepts_own_domain_and_subdomains():
    from app.services.rfp_express_builder import _same_site
    assert _same_site("www.ensigninfosecurity.com", "ensigninfosecurity.com") is True
    assert _same_site("legal.acme.sg", "acme.sg") is True
    assert _same_site("acme.sg", "www.acme.sg") is True


def test_privacy_url_backstop_blocks_google_and_allows_own():
    from app.services.rfp_express_builder import _privacy_url_belongs_to_site
    vendor = "https://www.ensigninfosecurity.com/"
    # The exact leak seen in the kit must be rejected.
    assert _privacy_url_belongs_to_site(
        "https://policies.google.com/privacy?hl=en-US", vendor) is False
    # The vendor's own policy is accepted.
    assert _privacy_url_belongs_to_site(
        "https://www.ensigninfosecurity.com/privacy-policy", vendor) is True
    # Missing inputs are safe (never presented).
    assert _privacy_url_belongs_to_site(None, vendor) is False
    assert _privacy_url_belongs_to_site("https://x.com/privacy", None) is False


# ── UEN back-fill: a name-only ACRA match must populate the UEN field ─────────
# Bug: kit printed "UEN: Not provided" though ACRA had resolved the entity and
# returned its UEN.

def test_acra_merge_backfills_uen_from_name_only_match():
    ctx = {"uen": None}  # buyer gave no UEN; ACRA matched by company name
    acra_live = {
        "found": True, "live": True, "uen": "201012345A",
        "registered_name": "ENSIGN INFOSECURITY (ASIA PACIFIC) PTE. LTD.",
        "entity_type": "Local Company", "entity_status": "Live Company",
    }
    RFPExpressBuilder._merge_acra_into_ctx(ctx, acra_live)
    assert ctx["uen"] == "201012345A"
    assert ctx["acra_status"] == "Live Company"
    assert ctx["acra_name"].startswith("ENSIGN INFOSECURITY")


def test_acra_merge_never_overwrites_intake_supplied_uen():
    ctx = {"uen": "999999999Z"}  # buyer supplied their own UEN
    acra_live = {"found": True, "live": True, "uen": "201012345A"}
    RFPExpressBuilder._merge_acra_into_ctx(ctx, acra_live)
    assert ctx["uen"] == "999999999Z", "intake-supplied UEN must win over ACRA"
