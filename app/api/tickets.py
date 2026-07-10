from app.core.route_classes import RetryAPIRoute
from datetime import datetime, timedelta, timezone
from typing import Optional
import asyncio
import html as _html
import logging
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.models import SupportTicket, SupportTicketReply
from app.services.email_service import EmailService
from app.services.email_layout import branded_email_html
from app.api.admin import _admin_auth

logger = logging.getLogger(__name__)

router = APIRouter(route_class=RetryAPIRoute)


class TicketCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=255)
    email: EmailStr
    category: str = Field(..., min_length=2, max_length=50)
    subject: str = Field(..., min_length=3, max_length=500)
    message: str = Field(..., min_length=3, max_length=5000)
    honeypot: Optional[str] = ""


class TicketResponse(BaseModel):
    status: str
    ticket_id: str
    tracking_url: str


class ReplyCreate(BaseModel):
    ticket_id: str = Field(..., min_length=6, max_length=50)
    message: str = Field(..., min_length=2, max_length=5000)
    is_internal: bool = False


def _send_email(to_address: str, subject: str, inner_html: str, *, title: str = "") -> None:
    """Send a support email through EmailService (Resend primary, SES fallback).

    ``inner_html`` is inner content — it is wrapped in the shared brand shell
    here. Best-effort: never raises so it can't block ticket creation.
    """
    try:
        body_html = branded_email_html(inner_html, title=title or subject)
        asyncio.run(
            EmailService().send_html_email(
                to_email=to_address,
                subject=subject,
                body_html=body_html,
            )
        )
    except Exception as exc:
        # Fail silently to avoid blocking ticket creation
        logger.warning(f"[Tickets] email to {to_address} failed: {exc}")
        return


def _rate_limit_exceeded(db, ip_address: str) -> bool:
    if not ip_address:
        return False
    window_start = datetime.now(timezone.utc) - timedelta(hours=1)
    recent_count = (
        db.query(func.count(SupportTicket.id))
        .filter(SupportTicket.ip_address == ip_address)
        .filter(SupportTicket.created_at >= window_start)
        .scalar()
    )
    return bool(recent_count and recent_count >= 3)


@router.post("/submit", response_model=TicketResponse)
def submit_ticket(request: Request, payload: TicketCreate):
    # Honeypot check (fake success)
    if payload.honeypot:
        return TicketResponse(
            status="success",
            ticket_id="BOP-SPAM",
            tracking_url="https://booppa.io/support",
        )

    db = SessionLocal()
    try:
        ip_address = request.client.host if request.client else None
        if _rate_limit_exceeded(db, ip_address):
            raise HTTPException(status_code=429, detail="Too many requests")

        ticket_code = f"BOP-{uuid.uuid4().hex[:8].upper()}"
        tracking_token = secrets.token_urlsafe(32)

        priority = "medium"
        urgent_keywords = ["urgent", "critical", "down", "broken", "emergency"]
        combined = f"{payload.subject} {payload.message}".lower()
        if any(k in combined for k in urgent_keywords):
            priority = "high"

        ticket = SupportTicket(
            ticket_id=ticket_code,
            tracking_token=tracking_token,
            name=payload.name.strip(),
            email=str(payload.email),
            category=payload.category.strip(),
            subject=payload.subject.strip(),
            message=payload.message.strip(),
            priority=priority,
            status="open",
            ip_address=ip_address,
            user_agent=request.headers.get("user-agent", ""),
        )
        db.add(ticket)
        db.commit()

        tracking_url = f"https://booppa.io/support/track/{ticket_code}?token={tracking_token}"

        # Notify support and user (best-effort)
        _p = 'style="margin:0 0 10px;color:#334155;font-size:15px;line-height:1.6;"'
        _send_email(
            settings.SUPPORT_EMAIL,
            f"[{ticket_code}] {payload.subject}",
            f"""
            <h2 style="margin:0 0 16px;font-size:20px;color:#0f172a;">New Support Ticket</h2>
            <p {_p}><strong>ID:</strong> {ticket_code}</p>
            <p {_p}><strong>From:</strong> {_html.escape(payload.name)} ({_html.escape(str(payload.email))})</p>
            <p {_p}><strong>Category:</strong> {_html.escape(payload.category)}</p>
            <p {_p}><strong>Priority:</strong> {priority}</p>
            <p {_p}><strong>Subject:</strong> {_html.escape(payload.subject)}</p>
            <p {_p}><strong>Message:</strong></p>
            <p {_p}>{_html.escape(payload.message)}</p>
            """,
            title="New support ticket",
        )
        _send_email(
            str(payload.email),
            f"Ticket {ticket_code} received",
            f"""
            <h2 style="margin:0 0 16px;font-size:20px;color:#0f172a;">Thanks for contacting BOOPPA Support</h2>
            <p {_p}>We’ve received your ticket <strong>{ticket_code}</strong>.</p>
            <p {_p}><strong>Subject:</strong> {_html.escape(payload.subject)}</p>
            <p {_p}>Track your ticket: <a href="{tracking_url}" style="color:#10b981;word-break:break-all;">{tracking_url}</a></p>
            """,
            title="Ticket received",
        )

        return TicketResponse(status="success", ticket_id=ticket_code, tracking_url=tracking_url)
    finally:
        db.close()


@router.get("/track/{ticket_id}")
def track_ticket(ticket_id: str, token: str):
    db = SessionLocal()
    try:
        ticket = (
            db.query(SupportTicket)
            .filter(SupportTicket.ticket_id == ticket_id)
            .filter(SupportTicket.tracking_token == token)
            .first()
        )
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")

        replies = (
            db.query(SupportTicketReply)
            .filter(SupportTicketReply.ticket_id == ticket_id)
            .filter(SupportTicketReply.is_internal == False)
            .order_by(SupportTicketReply.created_at.asc())
            .all()
        )

        return {
            "ticket": {
                "id": ticket.ticket_id,
                "subject": ticket.subject,
                "status": ticket.status,
                "priority": ticket.priority,
                "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
                "updated_at": ticket.updated_at.isoformat() if ticket.updated_at else None,
            },
            "replies": [
                {
                    "author": r.author,
                    "message": r.message,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in replies
            ],
        }
    finally:
        db.close()


@router.post("/reply")
def add_reply(payload: ReplyCreate, request: Request, _auth: bool = Depends(_admin_auth)):
    db = SessionLocal()
    try:
        ticket = db.query(SupportTicket).filter(SupportTicket.ticket_id == payload.ticket_id).first()
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")

        reply = SupportTicketReply(
            ticket_id=payload.ticket_id,
            author="BOOPPA Support",
            author_type="staff",
            message=payload.message,
            is_internal=payload.is_internal,
        )
        db.add(reply)
        ticket.status = "in_progress"
        ticket.updated_at = datetime.now(timezone.utc)
        db.commit()

        if not payload.is_internal:
            tracking_url = f"https://booppa.io/support/track/{ticket.ticket_id}?token={ticket.tracking_token}"
            _p = 'style="margin:0 0 10px;color:#334155;font-size:15px;line-height:1.6;"'
            _send_email(
                ticket.email,
                f"Ticket update {ticket.ticket_id}",
                f"""
                <h2 style="margin:0 0 16px;font-size:20px;color:#0f172a;">New update on your ticket</h2>
                <p {_p}><strong>Ticket:</strong> {ticket.ticket_id}</p>
                <p {_p}><strong>Message:</strong></p>
                <p {_p}>{_html.escape(payload.message)}</p>
                <p {_p}>Track: <a href="{tracking_url}" style="color:#10b981;word-break:break-all;">{tracking_url}</a></p>
                """,
                title="Ticket update",
            )

        return {"status": "success"}
    finally:
        db.close()