"""
scan_consent.py — Gate function for explicit consent (never scan without consent).
"""

from typing import Optional


def has_active_consent(
    db,
    *,
    csp_client_id: Optional[str] = None,
    managed_entity_id: Optional[str] = None,
    required_source: str,
) -> bool:
    """
    Call at the START of every enrichment module (address screening, UBO
    graph, future cyber-risk) — never after. required_source is one of:
    "acra_lookup", "domain_scan", "address_screening", "ubo_graph".

    Returns False (does not raise) if consent is missing or revoked —
    what to do with a False (skip the vendor, mark it "unverifiable",
    block onboarding) is the caller's decision; this function only
    answers "am I allowed to?".
    """
    from app.core.models import ScanConsentRecord

    if not csp_client_id and not managed_entity_id:
        raise ValueError("exactly one of csp_client_id or managed_entity_id is required")

    column_map = {
        "acra_lookup": ScanConsentRecord.consent_acra_lookup,
        "domain_scan": ScanConsentRecord.consent_domain_scan,
        "address_screening": ScanConsentRecord.consent_address_screening,
        "ubo_graph": ScanConsentRecord.consent_ubo_graph,
    }
    if required_source not in column_map:
        raise ValueError(f"unknown required_source: {required_source}")

    q = db.query(ScanConsentRecord).filter(
        ScanConsentRecord.revoked_at.is_(None),
        column_map[required_source].is_(True),
    )
    if csp_client_id:
        q = q.filter(ScanConsentRecord.csp_client_id == csp_client_id)
    else:
        q = q.filter(ScanConsentRecord.managed_entity_id == managed_entity_id)

    return db.query(q.exists()).scalar()
