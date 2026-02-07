import logging
from typing import Any, Dict


logger = logging.getLogger(__name__)


def check_access(assessment_data: Dict[str, Any] | None) -> Dict[str, Any]:
    data = assessment_data if isinstance(assessment_data, dict) else {}

    status_value = (
        data.get("access_status")
        or data.get("subscription_status")
        or data.get("plan_status")
        or ""
    )
    status = str(status_value).strip().lower()
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

    if status in blocked_statuses:
        return {"allowed": False, "paid": False, "reason": f"status:{status}"}

    if data.get("free_limit_reached") or data.get("plan_limit_reached"):
        return {"allowed": False, "paid": False, "reason": "limit_reached"}

    paid = bool(data.get("payment_confirmed"))
    plan = str(data.get("plan") or data.get("tier") or "").strip().lower()
    if plan in {"pro", "paid", "standard", "enterprise", "business"}:
        paid = True

    subscription_status = str(data.get("subscription_status") or "").strip().lower()
    if subscription_status in {"active", "trialing"}:
        paid = True

    return {"allowed": True, "paid": paid, "reason": None}
