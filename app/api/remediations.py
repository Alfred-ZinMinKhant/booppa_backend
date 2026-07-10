from __future__ import annotations
from app.core.route_classes import RetryAPIRoute
"""
Remediation Tracking API
========================
Lets vendors mark a specific PDPA finding as fixed (or won't-fix) from the
report view. The next completed scan for the same vendor + framework auto-
confirms the remediation: if the finding is gone we set
`confirmation_status='confirmed'`; if it still appears we set 'regressed'.

Endpoints (all under `/remediations` prefix):

  POST   /remediations/reports/{report_id}
         body: {finding_key: str, status: "fixed"|"wontfix", notes?: str}
         → creates a remediation row

  GET    /remediations/reports/{report_id}
         → list remediations for a specific report

  GET    /remediations/me
         → list ALL remediations for the current user, newest first

  PATCH  /remediations/{remediation_id}
         body: {status?: str, notes?: str}
         → update fields the user can change
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db, get_current_user
from app.core.models import Report, User
from app.core.models import FindingRemediation
from app.services.finding_keys import (
    extract_finding_keys,
    is_key_present,
    label_for_key,
)

logger = logging.getLogger(__name__)
router = APIRouter(route_class=RetryAPIRoute)

ALLOWED_STATUS = {"fixed", "wontfix", "open"}


class CreateRemediationRequest(BaseModel):
    finding_key: str = Field(..., max_length=128)
    status: str = Field(default="fixed", max_length=32)
    notes: Optional[str] = Field(default=None, max_length=2000)


class UpdateRemediationRequest(BaseModel):
    status: Optional[str] = Field(default=None, max_length=32)
    notes: Optional[str] = Field(default=None, max_length=2000)


class RemediationResponse(BaseModel):
    id: UUID
    vendor_id: UUID
    report_id: Optional[UUID]
    finding_key: str
    label: str
    status: str
    confirmation_status: str
    marked_at: datetime
    confirmed_at: Optional[datetime]
    confirming_report_id: Optional[UUID]
    notes: Optional[str]


def _to_response(r: FindingRemediation) -> RemediationResponse:
    return RemediationResponse(
        id=r.id,
        vendor_id=r.vendor_id,
        report_id=r.report_id,
        finding_key=r.finding_key,
        label=label_for_key(r.finding_key),
        status=r.status,
        confirmation_status=r.confirmation_status,
        marked_at=r.marked_at,
        confirmed_at=r.confirmed_at,
        confirming_report_id=r.confirming_report_id,
        notes=r.notes,
    )


@router.post(
    "/reports/{report_id}",
    response_model=RemediationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_remediation(
    report_id: UUID,
    body: CreateRemediationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mark a finding from a specific report as fixed/wontfix.

    The finding_key must be present in the report's assessment_data (so users
    can't mark phantom findings). Duplicate (vendor, finding_key) entries with
    status='fixed' are coalesced — we keep the existing row instead of inserting
    a new one, so a "fix" can be re-asserted idempotently.
    """
    if body.status not in ALLOWED_STATUS:
        raise HTTPException(400, f"status must be one of {sorted(ALLOWED_STATUS)}")

    report = db.query(Report).filter(
        Report.id == report_id,
        Report.owner_id == current_user.id,
    ).first()
    if not report:
        raise HTTPException(404, "report not found")

    if not is_key_present(report.assessment_data, body.finding_key):
        raise HTTPException(
            422,
            f"finding_key '{body.finding_key}' is not present in this report",
        )

    # Coalesce: re-marking an existing open remediation is idempotent
    existing = (
        db.query(FindingRemediation)
        .filter(
            FindingRemediation.vendor_id == current_user.id,
            FindingRemediation.finding_key == body.finding_key,
            FindingRemediation.confirmation_status.in_(("pending", "regressed")),
        )
        .order_by(FindingRemediation.marked_at.desc())
        .first()
    )
    if existing:
        existing.status = body.status
        if body.notes is not None:
            existing.notes = body.notes
        existing.marked_at = datetime.now(timezone.utc)
        existing.report_id = report_id
        # Re-marking a regressed remediation puts it back to pending
        existing.confirmation_status = "pending"
        existing.confirmed_at = None
        existing.confirming_report_id = None
        db.commit()
        db.refresh(existing)
        return _to_response(existing)

    row = FindingRemediation(
        vendor_id=current_user.id,
        report_id=report_id,
        finding_key=body.finding_key,
        status=body.status,
        confirmation_status="pending",
        marked_at=datetime.now(timezone.utc),
        marked_by_user_id=current_user.id,
        notes=body.notes,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_response(row)


@router.get(
    "/reports/{report_id}",
    response_model=list[RemediationResponse],
)
def list_remediations_for_report(
    report_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Remediations the current user has filed against this specific report.

    Also includes any historical remediations whose `finding_key` still appears
    in the report so the UI can hint "you marked this fixed N days ago".
    """
    report = db.query(Report).filter(
        Report.id == report_id,
        Report.owner_id == current_user.id,
    ).first()
    if not report:
        raise HTTPException(404, "report not found")

    keys_in_report = extract_finding_keys(report.assessment_data)

    rows = (
        db.query(FindingRemediation)
        .filter(FindingRemediation.vendor_id == current_user.id)
        .filter(
            (FindingRemediation.report_id == report_id)
            | (FindingRemediation.finding_key.in_(keys_in_report))
        )
        .order_by(FindingRemediation.marked_at.desc())
        .all()
    )
    return [_to_response(r) for r in rows]


@router.get("/me", response_model=list[RemediationResponse])
def list_my_remediations(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """All remediations the current user has ever filed, newest first.

    The frontend can group these by confirmation_status to show
    "Fixes you've confirmed" vs "Pending verification" vs "Regressed".
    """
    rows = (
        db.query(FindingRemediation)
        .filter(FindingRemediation.vendor_id == current_user.id)
        .order_by(FindingRemediation.marked_at.desc())
        .all()
    )
    return [_to_response(r) for r in rows]


@router.patch("/{remediation_id}", response_model=RemediationResponse)
def update_remediation(
    remediation_id: UUID,
    body: UpdateRemediationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = db.query(FindingRemediation).filter(
        FindingRemediation.id == remediation_id,
        FindingRemediation.vendor_id == current_user.id,
    ).first()
    if not row:
        raise HTTPException(404, "remediation not found")
    if body.status is not None:
        if body.status not in ALLOWED_STATUS:
            raise HTTPException(400, f"status must be one of {sorted(ALLOWED_STATUS)}")
        row.status = body.status
    if body.notes is not None:
        row.notes = body.notes
    db.commit()
    db.refresh(row)
    return _to_response(row)
