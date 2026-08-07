"""
scout_approval_api.py
Booppa Smart Care LLC — SCOUT Agents, approval endpoints

Deliberately small. This is not a CRM UI — it's the minimum surface
needed for a human to review a list and approve/reject in bulk, linked
from the weekly digest email.

Gated on the shared admin dependency (app/core/admin_auth.admin_auth) —
the same Bearer-admin-JWT / X-Admin-Token / HTTP-Basic rules the rest of
/admin runs on. Deliberately NOT get_current_user: that authenticates a
customer, and approving outbound cold email is a Booppa-team action.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from datetime import datetime, timezone

from app.core.admin_auth import admin_auth
from app.core.db import get_db
from app.core.models import ScoutProspect
from app.services.scout_outreach_templates import generate_vendor_outreach, generate_buyer_outreach, generate_csp_outreach

router = APIRouter(tags=["scout-agents"])


class ProspectSummary(BaseModel):
    id: str
    pipeline: str
    display_name: str
    score: int | None
    priority_tier: str | None
    website_url: str | None
    has_contact_email: bool
    outreach_subject: str | None


class BulkActionRequest(BaseModel):
    prospect_ids: list[str]
    reason: str | None = None  # required in practice for REJECTED, optional for APPROVED


@router.get("/pending", response_model=list[ProspectSummary])
async def list_pending(
    pipeline: str | None = None,
    db=Depends(get_db), _auth: bool = Depends(admin_auth),
):
    """What the weekly digest links to. Generates the outreach email lazily,
    the first time a prospect is viewed here."""
    q = db.query(ScoutProspect).filter(ScoutProspect.status == "PENDING_APPROVAL")
    if pipeline:
        q = q.filter(ScoutProspect.pipeline == pipeline)
    prospects = q.order_by(ScoutProspect.priority_tier.asc(), ScoutProspect.score.desc().nullslast()).all()

    for p in prospects:
        if not p.outreach_body_html:
            data = p.raw_data or {}
            if p.pipeline == "vendor":
                subject, body = generate_vendor_outreach(data, "", 0)
            elif p.pipeline == "buyer":
                subject, body = generate_buyer_outreach(data)
            else:
                subject, body = generate_csp_outreach(data)
            p.outreach_subject, p.outreach_body_html = subject, body
    db.commit()

    return [
        ProspectSummary(
            id=str(p.id), pipeline=p.pipeline, display_name=p.display_name,
            score=p.score, priority_tier=p.priority_tier, website_url=p.website_url,
            has_contact_email=bool((p.raw_data or {}).get("contact_email")),
            outreach_subject=p.outreach_subject,
        ) for p in prospects
    ]


@router.get("/pending/{prospect_id}/preview")
async def preview_prospect(
    prospect_id: str, db=Depends(get_db), _auth: bool = Depends(admin_auth),
):
    """Full email body, for a human to read before approving."""
    p = db.query(ScoutProspect).filter(ScoutProspect.id == prospect_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Prospect not found")
    return {
        "id": str(p.id), "display_name": p.display_name, "pipeline": p.pipeline,
        "contact_email": (p.raw_data or {}).get("contact_email"),
        "subject": p.outreach_subject, "body_html": p.outreach_body_html,
        "raw_data": p.raw_data,
    }


@router.post("/approve")
async def approve_batch(
    body: BulkActionRequest, db=Depends(get_db), _auth: bool = Depends(admin_auth),
):
    """Bulk approve — normal usage from weekly digest."""
    updated = (
        db.query(ScoutProspect)
        .filter(ScoutProspect.id.in_(body.prospect_ids), ScoutProspect.status == "PENDING_APPROVAL")
        .update({
            "status": "APPROVED",
            "approved_at": datetime.now(timezone.utc),
            # Admin auth is not a User row, so there is no id to record here.
            # approved_by_user_id stays NULL; the approval timestamp is the audit trail.
        }, synchronize_session=False)
    )
    db.commit()
    return {"approved": updated}


@router.post("/reject")
async def reject_batch(
    body: BulkActionRequest, db=Depends(get_db), _auth: bool = Depends(admin_auth),
):
    """Permanent no — marks status REJECTED with reason."""
    updated = (
        db.query(ScoutProspect)
        .filter(ScoutProspect.id.in_(body.prospect_ids))
        .update({"status": "REJECTED", "status_reason": body.reason or "rejected without reason given"},
                synchronize_session=False)
    )
    db.commit()
    return {"rejected": updated}
