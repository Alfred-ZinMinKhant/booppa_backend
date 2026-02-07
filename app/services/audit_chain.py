from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.core.models import AuditChainEvent


def append_audit_event(
    db: Session,
    report_id: str,
    action: str,
    actor: str,
    hash_value: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> AuditChainEvent:
    previous = (
        db.query(AuditChainEvent)
        .filter(AuditChainEvent.report_id == report_id)
        .order_by(AuditChainEvent.created_at.desc())
        .first()
    )
    prev_hash = previous.hash if previous else "GENESIS"

    event = AuditChainEvent(
        report_id=report_id,
        action=action,
        actor=actor,
        hash_prev=prev_hash,
        hash=hash_value,
        metadata_json=metadata or {},
        created_at=datetime.utcnow(),
    )
    db.add(event)
    return event
