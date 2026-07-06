"""Phase 2 invariants for the Compliance Evidence Pack (BCEP).

Two forensic findings drove these:

  1. A 5/7 pack shipped as "ready" — two governance docs (Training, Security
     Review Log) were missing but the pack still delivered and was emailed.
  2. The cover sheet listed the missing Security Review Log as "anchored"
     while the customer never received the file.

The delivery gate must require all SEVEN DOC_META doc types to be *both*
generated and present in the delivered download set; the cover-sheet BCEP
linkage must only list a doc that is both anchored AND delivered.
"""
from app.services.evidence_pack import DOC_META


REQUIRED = set(DOC_META.keys())


def test_pack_defines_exactly_seven_core_docs():
    # The product is sold as seven documents — the required set is the contract.
    assert len(REQUIRED) == 7, f"expected 7 BCEP docs, got {sorted(REQUIRED)}"


def _is_complete(documents: dict, download_urls: dict) -> bool:
    """Mirror of the tasks.py completeness gate: every required doc must be
    generated AND delivered."""
    generated = set(documents.keys())
    delivered = set(download_urls.keys())
    return not ((REQUIRED - generated) or (REQUIRED - delivered))


def test_incomplete_when_a_doc_failed_to_generate():
    docs = {dt: {} for dt in REQUIRED if dt != "review_log"}  # 6/7 generated
    urls = {dt: "https://s3/x" for dt in docs}
    assert _is_complete(docs, urls) is False


def test_incomplete_when_generated_but_upload_dropped():
    docs = {dt: {} for dt in REQUIRED}  # all 7 generated
    urls = {dt: "https://s3/x" for dt in REQUIRED if dt != "review_log"}  # 6/7 delivered
    assert _is_complete(docs, urls) is False


def test_complete_when_all_seven_generated_and_delivered():
    docs = {dt: {} for dt in REQUIRED}
    urls = {dt: "https://s3/x" for dt in REQUIRED}
    assert _is_complete(docs, urls) is True


def _cover_sheet_lists(anchoring: dict, download_urls: dict) -> set:
    """Mirror of the tasks.py BCEP→cover-sheet linkage: list a doc only when it
    is anchored with a real on-chain tx AND present in the delivered set."""
    from app.services.tx_utils import is_real_onchain_tx

    listed = set()
    for dt in DOC_META:
        anc = anchoring.get(dt) if isinstance(anchoring.get(dt), dict) else {}
        if not is_real_onchain_tx(anc.get("tx_hash")):
            continue
        if not download_urls.get(dt):
            continue
        listed.add(dt)
    return listed


def test_cover_sheet_omits_anchored_but_undelivered_doc():
    real_tx = "0x" + "a" * 64
    anchoring = {"review_log": {"tx_hash": real_tx}, "dpmp": {"tx_hash": real_tx}}
    download_urls = {"dpmp": "https://s3/x"}  # review_log anchored but not delivered
    listed = _cover_sheet_lists(anchoring, download_urls)
    assert "review_log" not in listed
    assert "dpmp" in listed


def test_cover_sheet_omits_doc_with_non_real_tx():
    anchoring = {"dpmp": {"tx_hash": "admin-sim-abc"}}
    download_urls = {"dpmp": "https://s3/x"}
    assert _cover_sheet_lists(anchoring, download_urls) == set()
