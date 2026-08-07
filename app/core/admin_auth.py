"""
Shared admin authentication dependency.

Extracted from app/api/admin.py so that other admin-only routers
(e.g. the SCOUT approval endpoints) gate on exactly the same rules
instead of re-implementing them or falling back to get_current_user,
which authenticates a *customer*, not a Booppa team member.
"""

import logging
import secrets

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.core.auth import verify_admin_token
from app.core.config import settings

logger = logging.getLogger(__name__)

security = HTTPBasic(auto_error=False)


def admin_auth(
    request: Request, credentials: HTTPBasicCredentials = Depends(security)
):
    """Accept (in order): Bearer admin JWT, X-Admin-Token header, or HTTP Basic creds."""
    # 1. Bearer admin JWT (used by the new /admin/login flow)
    auth_header = request.headers.get("authorization") or ""
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        payload = verify_admin_token(token)
        if payload and payload.get("sub"):
            return True

    # 2. Static X-Admin-Token header (legacy machine-to-machine)
    header = request.headers.get("x-admin-token")
    if settings.ADMIN_TOKEN:
        if header and secrets.compare_digest(header, settings.ADMIN_TOKEN):
            return True

    # 3. HTTP Basic (for direct API hits / curl)
    if settings.ADMIN_USER and settings.ADMIN_PASSWORD and credentials:
        valid_user = secrets.compare_digest(credentials.username, settings.ADMIN_USER)
        valid_pass = secrets.compare_digest(credentials.password, settings.ADMIN_PASSWORD)
        if valid_user and valid_pass:
            return True

    logger.warning("Admin authentication failed")
    raise HTTPException(status_code=401, detail="Unauthorized")
