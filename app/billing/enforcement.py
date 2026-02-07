from typing import Any, Dict


FREE = "FREE"
PRO = "PRO"
ENTERPRISE = "ENTERPRISE"

FREE_FRAMEWORKS = {"pdpa_free_scan"}

PRO_PRODUCT_KEYS = {
    "pdpa_quick_scan",
    "pdpa_basic",
    "pdpa_pro",
    "compliance_standard",
    "compliance_pro",
    "supply_chain_1",
    "supply_chain_10",
    "supply_chain_50",
    "compliance_notarization_1",
    "compliance_notarization_10",
    "compliance_notarization_50",
}


def _normalize(value: Any) -> str:
    return str(value or "").strip().lower()


def resolve_tier(assessment_data: Dict[str, Any] | None, framework: str | None) -> str:
    data = assessment_data if isinstance(assessment_data, dict) else {}
    framework_value = _normalize(framework)

    if tier in {"enterprise", "ent", "enterprise_monthly"}:
        return ENTERPRISE

    if product_type in PRO_PRODUCT_KEYS:
        return PRO

    if tier in {"pro", "paid", "standard", "business"}:
        return PRO

    if framework_value in FREE_FRAMEWORKS:
        return FREE
    
    if tier in {"free", "starter", "trial"}:
        return FREE

    if framework_value in {"pdpa_quick_scan", "pdpa_basic", "pdpa_pro"}:
        return PRO

    return FREE


def enforce_tier(
    assessment_data: Dict[str, Any] | None, framework: str | None
) -> Dict[str, Any]:
    data = assessment_data if isinstance(assessment_data, dict) else {}

    status_value = _normalize(
        data.get("access_status")
        or data.get("subscription_status")
        or data.get("plan_status")
    )
    blocked_statuses = {
        "blocked",
        "denied",
        "suspended",
        "past_due",
        "canceled",
        "cancelled",
        "limit_reached",
        "disabled",
    }

    if status_value in blocked_statuses:
        return {
            "allowed": False,
            "tier": resolve_tier(data, framework),
            "paid": False,
            "reason": f"status:{status_value}",
            "features": {},
        }

    if data.get("free_limit_reached") or data.get("plan_limit_reached"):
        return {
            "allowed": False,
            "tier": resolve_tier(data, framework),
            "paid": False,
            "reason": "limit_reached",
            "features": {},
        }

    tier = resolve_tier(data, framework)
    subscription_status = _normalize(data.get("subscription_status"))
    paid = bool(data.get("payment_confirmed"))
    if subscription_status in {"active", "trialing"}:
        paid = True

    allow_blockchain = paid and tier in {PRO, ENTERPRISE}
    allow_pdf = paid and tier in {PRO, ENTERPRISE}
    ai_full = tier in {PRO, ENTERPRISE} and paid

    features = {
        "ai_mode": "full" if ai_full else "light",
        "ai_full": ai_full,
        "pdf": allow_pdf,
        "blockchain": allow_blockchain,
        "monitoring": tier == ENTERPRISE,
        "dashboard": tier == ENTERPRISE,
        "multi_vendor": tier == ENTERPRISE,
    }

    return {
        "allowed": True,
        "tier": tier,
        "paid": paid,
        "reason": None,
        "features": features,
    }
