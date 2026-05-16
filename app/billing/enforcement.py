from typing import Any, Dict


FREE = "FREE"
PRO = "PRO"
ENTERPRISE = "ENTERPRISE"

FREE_FRAMEWORKS = {"pdpa_free_scan"}

PRO_PRODUCT_KEYS = {
    "pdpa_quick_scan",
    "compliance_notarization_1",
    "compliance_notarization_10",
    "compliance_notarization_50",
    "vendor_proof",
    "rfp_complete",
    "compliance_evidence_pack",
    "compliance_evidence_monthly",
    "pdpa_monitor_monthly",
    "pdpa_monitor_annual",
}

ENTERPRISE_PLAN_KEYS = {
    "enterprise", "ent", "enterprise_monthly", "enterprise_pro", "enterprise_pro_monthly",
    "standard_suite", "standard_suite_monthly",
    "pro_suite", "pro_suite_monthly",
    "evaluate_suppliers", "evaluate_suppliers_monthly",
    "verify_supplier_evidence", "verify_supplier_evidence_monthly",
}


def _normalize(value: Any) -> str:
    return str(value or "").strip().lower()


def resolve_tier(assessment_data: Dict[str, Any] | None, framework: str | None) -> str:
    data = assessment_data if isinstance(assessment_data, dict) else {}
    framework_value = _normalize(framework)

    tier = _normalize(data.get("tier") or data.get("plan") or data.get("package"))
    product_type = _normalize(data.get("product_type") or data.get("product"))

    if tier in ENTERPRISE_PLAN_KEYS or product_type in ENTERPRISE_PLAN_KEYS:
        return ENTERPRISE

    if product_type in PRO_PRODUCT_KEYS:
        return PRO

    if tier in {"pro", "paid", "standard", "business"}:
        return PRO

    if framework_value in FREE_FRAMEWORKS:
        return FREE
    
    if tier in {"free", "starter", "trial"}:
        return FREE

    if framework_value == "pdpa_quick_scan":
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

    plan_value = _normalize(data.get("plan") or data.get("tier") or data.get("package"))
    is_pro_suite = plan_value in {"pro_suite", "pro_suite_monthly", "enterprise_pro", "enterprise_pro_monthly"}
    is_standard_suite = plan_value in {"standard_suite", "standard_suite_monthly"}

    from app.core.models_v8 import ENTERPRISE_NOTARIZATION_LIMITS
    notarization_quota = ENTERPRISE_NOTARIZATION_LIMITS.get(plan_value, 0) if paid else 0

    features = {
        "ai_mode": "full" if ai_full else "light",
        "ai_full": ai_full,
        "pdf": allow_pdf,
        "blockchain": allow_blockchain,
        "monitoring": tier == ENTERPRISE,
        "dashboard": tier == ENTERPRISE,
        "multi_vendor": tier == ENTERPRISE,
        "api_access": tier in {PRO, ENTERPRISE} and paid,
        "webhooks": tier == ENTERPRISE and paid,
        "sso": is_pro_suite and paid,
        "white_label": is_pro_suite and paid,
        "monthly_notarization_quota": notarization_quota,
    }

    return {
        "allowed": True,
        "tier": tier,
        "paid": paid,
        "reason": None,
        "features": features,
    }
