from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.core.db import get_db
from app.core.models import ConsentLog
from datetime import datetime
import re

router = APIRouter()


class ConsentIn(BaseModel):
    timestamp: str
    consent_status: str
    policy_version: str | None = None
    metadata: dict | None = None


def anonymize_ip(ip: str | None) -> str | None:
    if not ip:
        return None
    # IPv4 anonymize last octet
    m = re.match(r"^(\d+\.\d+\.\d+)\.(\d+)$", ip)
    if m:
        return f"{m.group(1)}.0"
    # IPv6: zero out last 4 hextets
    parts = ip.split(":")
    if len(parts) >= 4:
        for i in range(len(parts) - 2, len(parts)):
            parts[i] = "0000"
        return ":".join(parts)
    return None


@router.post("/consent")
def record_consent(payload: ConsentIn, request: Request, db: Session = Depends(get_db)):
    try:
        # capture client IP
        client_ip = None
        if request.client:
            client_ip = request.client.host

        ip_anon = anonymize_ip(client_ip)

        timestamp = None
        try:
            timestamp = datetime.fromisoformat(payload.timestamp)
        except Exception:
            timestamp = datetime.utcnow()

        entry = ConsentLog(
            timestamp=timestamp,
            ip_anonymized=ip_anon,
            consent_status=payload.consent_status,
            policy_version=payload.policy_version,
            metadata=payload.metadata,
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        return {"detail": "recorded", "id": str(entry.id)}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
