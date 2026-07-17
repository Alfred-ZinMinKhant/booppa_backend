from app.core.route_classes import RetryAPIRoute
"""RFP intake endpoints for bundle buyers.

Bundle SKUs that include an RFP component defer kit generation: at webhook time
we create a PendingRfpIntake row and email the buyer a link. This module backs
those endpoints — list pending intakes and submit a brief.
"""
import logging
from datetime import datetime
from io import BytesIO

from fastapi import APIRouter, Depends, File, HTTPException, Security, UploadFile
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from app.core.auth import verify_access_token
from app.core.db import get_db
from app.core.models import User
from app.core.models import PendingRfpIntake

router = APIRouter(route_class=RetryAPIRoute)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)


def _resolve_user(token: str | None, db: Session) -> User:
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = verify_access_token(token)
    if not payload or not payload.get("sub"):
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.query(User).filter(User.email == payload.get("sub")).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.get("/pending")
def list_pending(
    token: str | None = Security(oauth2_scheme),
    db: Session = Depends(get_db),
):
    """List the authenticated user's pending RFP intakes (one per bundle purchase)."""
    user = _resolve_user(token, db)
    rows = (
        db.query(PendingRfpIntake)
        .filter(
            PendingRfpIntake.user_id == user.id,
            PendingRfpIntake.status == "pending",
        )
        .order_by(PendingRfpIntake.created_at.desc())
        .all()
    )
    return {
        "items": [
            {
                "id": str(r.id),
                "rfp_product_type": r.rfp_product_type,
                "bundle_source": r.bundle_source,
                "vendor_url": r.vendor_url,
                "company_name": r.company_name,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.get("/kits")
def list_kits(
    token: str | None = Security(oauth2_scheme),
    db: Session = Depends(get_db),
):
    """The authenticated user's generated RFP kits — a persistent library so a
    kit isn't a session-only dead-end. Each entry carries its download links and
    the session_id (for the re-presigning result page)."""
    user = _resolve_user(token, db)
    from app.core.models import Report

    rows = (
        db.query(Report)
        .filter(
            Report.owner_id == user.id,
            Report.framework.in_(["rfp_complete", "rfp_express"]),
            Report.status == "completed",
        )
        .order_by(Report.completed_at.desc().nullslast(), Report.created_at.desc())
        .limit(50)
        .all()
    )
    # Re-presign stored S3 links: the presigned URLs saved at generation time
    # expire when the signing STS credentials rotate (~hours on ECS roles), well
    # before their 7-day TTL. Backend redirect routes (e.g. docx_url) parse as
    # non-S3 and pass through unchanged.
    from app.services.storage import S3Service
    s3 = S3Service()

    items = []
    for r in rows:
        ad = r.assessment_data if isinstance(r.assessment_data, dict) else {}
        when = r.completed_at or r.created_at
        items.append({
            "reportId": str(r.id),
            "sessionId": ad.get("session_id"),
            "companyName": r.company_name or ad.get("company_name"),
            "vendorUrl": r.company_website or ad.get("vendor_url"),
            "productType": r.framework,
            "createdAt": when.isoformat() if when else None,
            "downloadUrl": s3.refresh_url(r.s3_url or ad.get("download_url") or ""),
            "docxUrl": s3.refresh_url(ad.get("docx_url") or ""),
            "declarationUrl": s3.refresh_url(ad.get("declaration_url") or ""),
            "appendixDUrl": s3.refresh_url(ad.get("appendix_d_url") or ""),
        })
    return {"items": items}


@router.get("/previous")
def previous_intake(
    token: str | None = Security(oauth2_scheme),
    db: Session = Depends(get_db),
):
    """The user's most recent RFP brief + intake, so a new RFP can be pre-filled
    instead of re-entering 30+ fields. Returns null fields when none exist."""
    user = _resolve_user(token, db)
    from app.core.models import Report

    prior = (
        db.query(Report)
        .filter(
            Report.owner_id == user.id,
            Report.framework.in_(["rfp_complete", "rfp_express"]),
            Report.status == "completed",
        )
        .order_by(Report.completed_at.desc().nullslast(), Report.created_at.desc())
        .first()
    )
    if not prior:
        return {"available": False}
    ad = prior.assessment_data if isinstance(prior.assessment_data, dict) else {}
    intake = ad.get("intake_data") if isinstance(ad.get("intake_data"), dict) else {}
    return {
        "available": bool(intake) or bool(ad.get("intake_rfp_description")),
        "companyName": prior.company_name or ad.get("company_name"),
        "vendorUrl": prior.company_website or ad.get("vendor_url"),
        "uen": (intake or {}).get("uen") or ad.get("uen"),
        "rfpDescription": ad.get("intake_rfp_description"),
        "intakeData": intake or {},
        "generatedAt": (prior.completed_at or prior.created_at).isoformat() if (prior.completed_at or prior.created_at) else None,
    }


@router.get("/{intake_id}")
def get_intake(
    intake_id: str,
    token: str | None = Security(oauth2_scheme),
    db: Session = Depends(get_db),
):
    """Fetch a single pending intake. Used by the intake page on /rfp-intake/{id}."""
    user = _resolve_user(token, db)
    row = (
        db.query(PendingRfpIntake)
        .filter(PendingRfpIntake.id == intake_id, PendingRfpIntake.user_id == user.id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Intake not found")
    # If the buyer used the pre-purchase /rfp-acceleration form, their
    # description + supplied facts were cached at session_id. Surface them as
    # `prefill` so the intake form can seed its inputs. Cache is best-effort:
    # missing/expired entries just yield an empty prefill, form starts blank.
    prefill: dict = {}
    if row.session_id:
        try:
            from app.core.cache import cache as cache_mod
            cached = cache_mod.get(cache_mod.cache_key(f"rfp_intake:{row.session_id}"))
            if isinstance(cached, dict):
                prefill = {
                    "rfp_description": cached.get("rfp_description") or "",
                    "intake_data": cached.get("intake_data") if isinstance(cached.get("intake_data"), dict) else {},
                }
        except Exception:
            pass  # cache miss / serialization issue — fall back to empty form

    return {
        "id": str(row.id),
        "rfp_product_type": row.rfp_product_type,
        "bundle_source": row.bundle_source,
        "vendor_url": row.vendor_url,
        "company_name": row.company_name,
        "status": row.status,
        # session_id lets the intake page route back to /rfp-acceleration/result
        # after submit so the buyer sees the kit polling/result, not just a
        # "go to dashboard" dead end.
        "session_id": row.session_id,
        "prefill": prefill,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "submitted_at": row.submitted_at.isoformat() if row.submitted_at else None,
    }


@router.post("/{intake_id}/submit")
def submit_intake(
    intake_id: str,
    body: dict,
    token: str | None = Security(oauth2_scheme),
    db: Session = Depends(get_db),
):
    """Submit the RFP brief and queue fulfill_rfp_task.

    Body: {
      rfp_description: str (required),
      vendor_url: str (required if not on row or user profile),
      company_name: str (required if not on row or user profile),
      intake_data?: dict,
      sector?: str,
    }

    vendor_url is mandatory because the kit generator scrapes it for
    ISO/SOC mentions, encryption language, sub-processor lists, and other
    signals that make answers fact-backed. Without it the kit can only
    label answers AI-drafted.
    """
    user = _resolve_user(token, db)
    row = (
        db.query(PendingRfpIntake)
        .filter(PendingRfpIntake.id == intake_id, PendingRfpIntake.user_id == user.id)
        .with_for_update()
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Intake not found")
    # "needs_more_info" means a prior submission was blocked at the placeholder
    # gate and the buyer is completing the missing facts — allow resubmission.
    if row.status not in ("pending", "needs_more_info"):
        raise HTTPException(status_code=409, detail="This RFP has already been submitted")

    rfp_description = (body.get("rfp_description") or "").strip()
    if not rfp_description:
        raise HTTPException(status_code=422, detail="rfp_description is required")
    intake_data = body.get("intake_data") if isinstance(body.get("intake_data"), dict) else None
    sector = body.get("sector")

    # Buyer-supplied vendor_url / company_name take precedence (they may have
    # changed since the row was created). Fall back to the row, then to the
    # User profile. If we still have nothing, refuse — we can't generate a
    # verified kit without a website to scan.
    vendor_url = (
        (body.get("vendor_url") or "").strip()
        or row.vendor_url
        or (getattr(user, "website", "") or "")
    ).strip()
    company_name = (
        (body.get("company_name") or "").strip()
        or row.company_name
        or (getattr(user, "company", "") or "")
    ).strip()
    if not vendor_url:
        raise HTTPException(
            status_code=422,
            detail="vendor_url is required — we need your public website to verify your compliance claims.",
        )
    if not company_name:
        raise HTTPException(status_code=422, detail="company_name is required.")

    # UEN is mandatory before generation (audit fix): it is the field GeBIZ
    # procurement officers check first, and kits previously shipped with UEN
    # "Not provided". Accept it from the intake_data, the body, the row, or the
    # user profile — but refuse if we still have nothing.
    uen = (
        ((intake_data or {}).get("uen") or "").strip()
        or (body.get("uen") or "").strip()
        or (getattr(row, "uen", "") or "")
        or (getattr(user, "uen", "") or "")
    ).strip()
    if not uen:
        raise HTTPException(
            status_code=422,
            detail="uen is required — your Singapore UEN (Business Registration No.) must appear on a GeBIZ-ready RFP Kit.",
        )
    # Ensure the UEN reaches the builder via intake_data (it reads intake['uen']).
    intake_data = {**(intake_data or {}), "uen": uen}

    # Persist on the row so the worker reads the latest values (the row's
    # original fields may be stale if the buyer changed them in the form).
    row.vendor_url = vendor_url
    row.company_name = company_name
    row.uen = uen
    row.status = "submitted"
    row.submitted_at = datetime.utcnow()
    db.commit()

    from app.workers.tasks import fulfill_rfp_task

    fulfill_rfp_task.delay(
        product_type=row.rfp_product_type,
        vendor_id=str(user.id),
        vendor_email=user.email,
        vendor_url=vendor_url,
        company_name=company_name,
        rfp_description=rfp_description,
        session_id=row.session_id,
        intake_data=intake_data,
    )

    # Strategy 6 — notify top sector peers, mirrors the standalone rfp_express path.
    if row.rfp_product_type == "rfp_express":
        try:
            from app.workers.tasks import fire_strategy_6_task

            fire_strategy_6_task.delay(sector, rfp_description)
        except Exception:
            pass

    return {
        "status": "queued",
        "intake_id": str(row.id),
        "session_id": row.session_id,
    }


@router.post("/{intake_id}/resolve")
def resolve_intake(
    intake_id: str,
    body: dict,
    token: str | None = Security(oauth2_scheme),
    db: Session = Depends(get_db),
):
    """Resolve a discrepancy on an already-generated RFP kit and regenerate.

    After a kit ships, the result may carry `discrepancies` (e.g. the buyer
    declared ISO 27001 but no public evidence was found). This lets the buyer
    supply the corrected fact(s) and regenerate without starting over: we merge
    the corrected `intake_data` over the prior intake (read from the last kit's
    Report) and re-queue fulfill_rfp_task on the same session.

    Body: { intake_data: dict (required, the corrected fields),
            rfp_description?: str (defaults to the prior brief) }
    """
    from app.core.models import Report

    user = _resolve_user(token, db)
    row = (
        db.query(PendingRfpIntake)
        .filter(PendingRfpIntake.id == intake_id, PendingRfpIntake.user_id == user.id)
        .with_for_update()
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Intake not found")

    corrected = body.get("intake_data")
    if not isinstance(corrected, dict) or not corrected:
        raise HTTPException(status_code=422, detail="intake_data with the corrected field(s) is required")

    # Recover the prior brief + intake from the most recent generated kit so the
    # regeneration keeps everything the buyer already provided.
    prior_report = (
        db.query(Report)
        .filter(
            Report.owner_id == user.id,
            Report.framework.in_(["rfp_complete", "rfp_express"]),
            Report.status == "completed",
        )
        .order_by(Report.created_at.desc())
        .first()
    )
    prior_ad = prior_report.assessment_data if (prior_report and isinstance(prior_report.assessment_data, dict)) else {}
    prior_intake = prior_ad.get("intake_data") if isinstance(prior_ad.get("intake_data"), dict) else {}
    rfp_description = (
        (body.get("rfp_description") or "").strip()
        or prior_ad.get("intake_rfp_description")
        or ""
    ).strip()

    merged_intake = {**prior_intake, **corrected}
    # Keep UEN/url/company stable from the row (set at submit time).
    if row.uen and not merged_intake.get("uen"):
        merged_intake["uen"] = row.uen
    vendor_url = (row.vendor_url or (getattr(user, "website", "") or "")).strip()
    company_name = (row.company_name or (getattr(user, "company", "") or "")).strip()
    if not rfp_description:
        raise HTTPException(status_code=422, detail="No prior RFP brief found to regenerate from — submit the intake first.")
    if not vendor_url:
        raise HTTPException(status_code=422, detail="vendor_url is required to regenerate.")

    row.status = "submitted"
    row.submitted_at = datetime.utcnow()
    db.commit()

    from app.workers.tasks import fulfill_rfp_task

    fulfill_rfp_task.delay(
        product_type=row.rfp_product_type,
        vendor_id=str(user.id),
        vendor_email=user.email,
        vendor_url=vendor_url,
        company_name=company_name,
        rfp_description=rfp_description,
        session_id=row.session_id,
        intake_data=merged_intake,
    )
    return {"status": "queued", "intake_id": str(row.id), "session_id": row.session_id}


# Cap: 10 MB upload, ~15k chars of extracted text downstream. Bigger than that
# and we're paying for tokens we'll truncate anyway.
_EXTRACT_MAX_FILE_BYTES = 10 * 1024 * 1024
_EXTRACT_MAX_PAGES = 25


@router.post("/{intake_id}/extract")
async def extract_tender_brief(
    intake_id: str,
    file: UploadFile = File(...),
    token: str | None = Security(oauth2_scheme),
    db: Session = Depends(get_db),
):
    """
    Extract a structured RFP brief from an uploaded tender PDF so the buyer
    can pre-fill the intake form instead of typing the description by hand.

    Returns a dict of suggested fields; the frontend merges them into the
    form state and the buyer reviews + edits before submitting. We never
    persist the extracted text — only the buyer's confirmed values land on
    the PendingRfpIntake row.

    Failure modes (return null + a hint, don't 500):
      - file isn't a PDF or pypdf can't read it
      - DeepSeek returns no parseable JSON
    """
    user = _resolve_user(token, db)

    # Verify the intake belongs to this user — same gate as submit_intake.
    row = (
        db.query(PendingRfpIntake)
        .filter(PendingRfpIntake.id == intake_id, PendingRfpIntake.user_id == user.id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Intake not found")

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Upload a PDF tender document.")

    contents = await file.read()
    if len(contents) == 0:
        raise HTTPException(status_code=400, detail="File is empty.")
    if len(contents) > _EXTRACT_MAX_FILE_BYTES:
        raise HTTPException(status_code=400, detail="PDF too large. Maximum 10 MB.")

    # Extract text via pypdf (same pattern as pdf_nric_scanner.py).
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.error("[ExtractTender] pypdf not installed")
        raise HTTPException(status_code=500, detail="PDF parsing not available.")

    try:
        reader = PdfReader(BytesIO(contents))
    except Exception as e:
        logger.info(f"[ExtractTender] PdfReader failed: {e}")
        return {
            "extracted": None,
            "reason": "Couldn't read this PDF — please type your brief manually.",
        }

    pages = reader.pages[:_EXTRACT_MAX_PAGES]
    parts: list[str] = []
    for p in pages:
        try:
            parts.append(p.extract_text() or "")
        except Exception:
            continue
    text = "\n".join(parts).strip()
    if len(text) < 80:
        return {
            "extracted": None,
            "reason": "We couldn't pull readable text from this PDF — please type your brief manually.",
        }

    # Call the extractor on BooppaAIService.
    try:
        from app.services.booppa_ai_service import BooppaAIService
        ai = BooppaAIService()
        suggested = await ai.extract_rfp_brief_from_tender_pdf(text)
    except Exception as e:
        logger.warning(f"[ExtractTender] AI extraction errored: {e}")
        return {
            "extracted": None,
            "reason": "Couldn't extract a brief automatically — please type yours.",
        }

    if not suggested or not suggested.get("rfp_description"):
        return {
            "extracted": None,
            "reason": "Couldn't extract a brief automatically — please type yours.",
        }

    return {
        "extracted": suggested,
        "source_filename": file.filename,
        "source_page_count": len(reader.pages),
    }
