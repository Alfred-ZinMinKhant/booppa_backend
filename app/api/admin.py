from fastapi import APIRouter, Request, HTTPException, Query, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from typing import List
from app.core.db import SessionLocal
from app.core.models import ConsentLog
from app.core.config import settings
import logging
import secrets

logger = logging.getLogger(__name__)

router = APIRouter()
security = HTTPBasic()


def _admin_auth(
    request: Request, credentials: HTTPBasicCredentials = Depends(security)
):
    """Allow either X-Admin-Token header or HTTP Basic credentials (ADMIN_USER/ADMIN_PASSWORD)."""
    # Check header token first
    header = request.headers.get("x-admin-token")
    if settings.ADMIN_TOKEN:
        if header and secrets.compare_digest(header, settings.ADMIN_TOKEN):
            return True

    # Fallback to HTTP Basic if configured
    if settings.ADMIN_USER and settings.ADMIN_PASSWORD:
        if credentials:
            valid_user = secrets.compare_digest(
                credentials.username, settings.ADMIN_USER
            )
            valid_pass = secrets.compare_digest(
                credentials.password, settings.ADMIN_PASSWORD
            )
            if valid_user and valid_pass:
                return True

    # Nothing matched
    logger.warning("Admin authentication failed")
    raise HTTPException(status_code=401, detail="Unauthorized")


@router.get("/consent/logs")
def list_consent_logs(
    request: Request,
    limit: int = Query(50, ge=1, le=1000),
    _auth: bool = Depends(_admin_auth),
) -> List[dict]:
    """Return recent consent logs for quick verification. Protected by admin auth."""

    db = SessionLocal()
    try:
        rows = (
            db.query(ConsentLog)
            .order_by(ConsentLog.timestamp.desc())
            .limit(limit)
            .all()
        )
        results = []
        for r in rows:
            results.append(
                {
                    "id": str(r.id),
                    "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                    "ip_anonymized": r.ip_anonymized,
                    "consent_status": r.consent_status,
                    "policy_version": r.policy_version,
                    "metadata": r.metadata_json,
                }
            )
        return results
    finally:
        db.close()
