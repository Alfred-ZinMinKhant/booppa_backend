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

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from datetime import datetime, timezone

from app.core.admin_auth import admin_auth
from app.core.auth import verify_admin_token
from app.core.db import get_db
from app.core.models import ScoutProspect, User
from app.services.scout_outreach_templates import generate_vendor_outreach, generate_buyer_outreach, generate_csp_outreach

router = APIRouter(tags=["scout-agents"])


def admin_identity(request: Request) -> str:
    """Who is acting, for the audit trail — never for authorisation.

    ``admin_auth`` returns only True/False and accepts two credential types, one
    of which (HTTP Basic) is a shared secret that identifies no individual. So
    this is best-effort: the JWT's subject when there is one, otherwise a label
    naming the credential class. It runs *alongside* ``admin_auth``, never
    instead of it — by itself it rejects nothing.

    The `X-Admin-Token` branch was dropped on 2026-08-08 along with the header
    itself; consent-sensitive approvals should carry an admin JWT so the audit
    trail names a person.
    """
    auth_header = request.headers.get("authorization") or ""
    if auth_header.lower().startswith("bearer "):
        payload = verify_admin_token(auth_header.split(" ", 1)[1].strip())
        if payload and payload.get("sub"):
            return str(payload["sub"])
    return "http-basic (shared credential, no individual identity)"


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
            # The generators return ("", "") when they cannot produce a legally
            # sendable message — no contact address to build an unsubscribe link
            # for, or COMPANY_POSTAL_ADDRESS unset. Leave the columns NULL in
            # that case rather than storing an empty body that reads like a
            # generation bug; the row stays visible here with
            # outreach_subject=None so the reason is chased rather than approved.
            if subject and body:
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


class ProspectEditRequest(BaseModel):
    subject: str | None = None
    body_html: str | None = None


@router.patch("/pending/{prospect_id}")
async def edit_prospect(
    prospect_id: str, body: ProspectEditRequest,
    db=Depends(get_db), _auth: bool = Depends(admin_auth),
):
    """Let the reviewer edit the outreach before approving it.

    The generated body opens with a neutral "Hello," because no pipeline
    captures a contact person's name. This is how a reviewer who does know the
    person personalises it — previously the stored body could not be edited at
    all, which is why the old templates shipped the literal "Dear [Name],".

    Only PENDING_APPROVAL rows are editable: what a human approved must be
    exactly what gets sent (the whole reason the body is stored rather than
    regenerated at send time). The send-side compliance gate still applies to
    whatever is saved here, so an edit cannot strip the <ADV> prefix,
    unsubscribe link or postal address and reach a recipient.
    """
    p = db.query(ScoutProspect).filter(ScoutProspect.id == prospect_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Prospect not found")
    if p.status != "PENDING_APPROVAL":
        raise HTTPException(
            status_code=409,
            detail=f"Only PENDING_APPROVAL prospects can be edited (this one is {p.status})",
        )
    if body.subject is not None:
        p.outreach_subject = body.subject[:255]
    if body.body_html is not None:
        p.outreach_body_html = body.body_html
    p.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {"id": str(p.id), "subject": p.outreach_subject, "body_html": p.outreach_body_html}


@router.post("/approve")
async def approve_batch(
    body: BulkActionRequest, db=Depends(get_db),
    _auth: bool = Depends(admin_auth), actor: str = Depends(admin_identity),
):
    """Bulk approve — normal usage from weekly digest."""
    # Approving cold outreach is a consent-sensitive act, so record who did it.
    # approved_by_user_id can only be filled when the admin JWT's subject maps
    # to a real User row; the shared-secret credential paths identify nobody, so
    # the identity is also written to status_reason in text — a NULL id there
    # means "not individually attributable", not "not recorded".
    actor_user = db.query(User.id).filter(User.email == actor.strip().lower()).first()
    updated = (
        db.query(ScoutProspect)
        .filter(ScoutProspect.id.in_(body.prospect_ids), ScoutProspect.status == "PENDING_APPROVAL")
        .update({
            "status": "APPROVED",
            "approved_at": datetime.now(timezone.utc),
            "approved_by_user_id": actor_user[0] if actor_user else None,
            "status_reason": f"approved by {actor}"[:500],
        }, synchronize_session=False)
    )
    db.commit()
    return {"approved": updated, "approved_by": actor}


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
