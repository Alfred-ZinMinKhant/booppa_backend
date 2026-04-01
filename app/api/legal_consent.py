from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional
from app.core.db import get_db
from app.core.models import HardenedConsent

router = APIRouter()


class HardenedConsentIn(BaseModel):
    user_email: str
    user_id: Optional[str] = None
    legal_version: str = "v17_Hardened"


@router.post("/legal/consent")
async def record_hardened_consent(
    payload: HardenedConsentIn,
    request: Request,
    db: Session = Depends(get_db),
):
    ip = request.headers.get("x-forwarded-for") or (
        request.client.host if request.client else None
    )
    if ip and "," in ip:
        ip = ip.split(",")[0].strip()

    user_id = None
    if payload.user_id:
        try:
            import uuid
            user_id = uuid.UUID(payload.user_id)
        except (ValueError, AttributeError):
            user_id = None

    record = HardenedConsent(
        user_email=payload.user_email,
        user_id=user_id,
        ip_address=ip,
        user_agent=request.headers.get("user-agent"),
        legal_version=payload.legal_version,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return {"detail": "recorded", "id": str(record.id)}
