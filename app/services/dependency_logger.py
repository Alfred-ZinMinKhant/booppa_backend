from datetime import datetime
from typing import Any, Dict


def log_dependency_event(
    assessment_data: Dict[str, Any] | None,
    owner_id: str | None,
    report_id: str,
    company_name: str | None = None,
    event_type: str = "report_completed",
    extra: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    data = assessment_data if isinstance(assessment_data, dict) else {}

    event = {
        "event": event_type,
        "owner_id": owner_id,
        "report_id": report_id,
        "company_name": company_name,
        "timestamp": datetime.utcnow().isoformat(),
    }
    if isinstance(extra, dict) and extra:
        event.update(extra)

    events = data.get("dependency_events")
    if not isinstance(events, list):
        events = []
    events.append(event)

    return {
        "dependency_events": events,
        "last_dependency_event": event,
    }
