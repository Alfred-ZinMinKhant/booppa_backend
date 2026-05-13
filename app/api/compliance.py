"""
Compliance Evidence Pack — dedicated endpoints
==============================================
Workflow: PDPA Snapshot + RFP Complete Kit auto-generate → Cover Sheet PDF emailed →
user signs PDF → user uploads signed PDF here (consumes their 1 dedicated
`compliance_evidence_credits`) → signed sheet anchored on-chain → cover sheet
regenerated with the signed-tx row populated → final blockchain receipt emailed.

Kept separate from /notarize so other bundle uploads cannot drain the CE credit.
"""

from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from typing import Optional
import hashlib
import logging
import uuid

from app.core.db import SessionLocal
from app.core.models import Report, User
from app.services.storage import S3Service

logger = logging.getLogger(__name__)
router = APIRouter()

ALLOWED_EXTENSIONS = {".pdf"}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


def _validate_signed_extension(filename: str) -> bool:
    return any(filename.lower().endswith(ext) for ext in ALLOWED_EXTENSIONS)


def _presign(key: str | None, expires: int = 604800) -> str | None:
    """Generate a fresh presigned GET URL for an S3 key. Returns None on failure."""
    if not key:
        return None
    try:
        s3 = S3Service()
        return s3.s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": s3.bucket, "Key": key},
            ExpiresIn=expires,
        )
    except Exception as e:
        logger.warning(f"[ComplianceStatus] presign failed for {key}: {e}")
        return None


@router.get("/cover-sheet/status")
async def cover_sheet_status(email: str):
    """
    One-stop status feed for the dedicated /compliance/cover-sheet page.
    Includes: pdpa, rfp, cover_sheet (issued PDF), signed (uploaded copy),
    credits balance, and the pending_cover_sheet flag.
    """
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            return {
                "credits": 0,
                "pending_cover_sheet": False,
                "signed_uploaded": False,
                "vendor_url_missing": False,
                "pdpa": None,
                "rfp": None,
                "cover_sheet": {"ready": False},
                "signed": None,
            }

        pdpa = (
            db.query(Report)
            .filter(
                Report.owner_id == user.id,
                Report.framework.in_(["pdpa_quick_scan", "pdpa_snapshot"]),
            )
            .order_by(Report.created_at.desc())
            .first()
        )
        pdpa_payload = None
        if pdpa:
            pdpa_ad = pdpa.assessment_data if isinstance(pdpa.assessment_data, dict) else {}
            structured = pdpa_ad.get("booppa_report") if isinstance(pdpa_ad.get("booppa_report"), dict) else {}
            structured_ra = (
                structured.get("risk_assessment")
                if isinstance(structured.get("risk_assessment"), dict)
                else {}
            )
            score_val = None
            raw_risk = (
                pdpa_ad.get("overall_risk_score")
                if pdpa_ad.get("overall_risk_score") is not None
                else pdpa_ad.get("score")
                if pdpa_ad.get("score") is not None
                else pdpa_ad.get("risk_score")
                if pdpa_ad.get("risk_score") is not None
                else structured_ra.get("score")
            )
            if raw_risk is not None:
                try:
                    score_val = max(0, min(100, 100 - int(raw_risk)))
                except (TypeError, ValueError):
                    score_val = None
            pdpa_payload = {
                "status": pdpa.status,
                "score": score_val,
                "completed_at": pdpa.completed_at.isoformat() if pdpa.completed_at else None,
            }

        rfp = (
            db.query(Report)
            .filter(Report.owner_id == user.id, Report.framework == "rfp_complete")
            .order_by(Report.created_at.desc())
            .first()
        )
        rfp_payload = None
        if rfp:
            rfp_ad = rfp.assessment_data if isinstance(rfp.assessment_data, dict) else {}
            # Presigned URLs expire after 7 days — re-presign from the stored
            # s3_key when available, falling back to the original URL otherwise.
            rfp_download = _presign(rfp_ad.get("s3_key")) or rfp_ad.get("download_url")
            rfp_payload = {
                "status": rfp.status,
                "completed_at": rfp.completed_at.isoformat() if rfp.completed_at else None,
                "download_url": rfp_download,
            }

        cs = (
            db.query(Report)
            .filter(Report.owner_id == user.id, Report.framework == "compliance_evidence_pack")
            .order_by(Report.created_at.desc())
            .first()
        )
        cs_payload = {"ready": False}
        if cs and (cs.file_key or cs.s3_url):
            cs_ad = cs.assessment_data if isinstance(cs.assessment_data, dict) else {}
            cs_key = cs.file_key or cs_ad.get("s3_key")
            cs_payload = {
                "ready": True,
                "download_url": _presign(cs_key) or cs.s3_url,
                "tx_hash": cs.tx_hash,
                "generated_at": cs.completed_at.isoformat() if cs.completed_at else None,
            }

        # Scope to current cycle: only show signed report from after the latest
        # PDPA scan (so monthly subscribers don't see last month's signed sheet
        # bleed into this cycle's UI).
        signed_q = db.query(Report).filter(
            Report.owner_id == user.id,
            Report.framework == "compliance_evidence_signed_sheet",
        )
        if pdpa and pdpa.created_at:
            signed_q = signed_q.filter(Report.created_at >= pdpa.created_at)
        signed = signed_q.order_by(Report.created_at.desc()).first()
        signed_payload = None
        if signed:
            s_ad = signed.assessment_data if isinstance(signed.assessment_data, dict) else {}
            signed_payload = {
                "uploaded_at": signed.created_at.isoformat() if signed.created_at else None,
                "tx_hash": signed.tx_hash,
                "file_hash": s_ad.get("file_hash") or signed.audit_hash,
                "file_name": s_ad.get("original_filename"),
            }

        website = (getattr(user, "website", "") or "").strip()
        return {
            "credits": getattr(user, "compliance_evidence_credits", 0) or 0,
            "pending_cover_sheet": bool(getattr(user, "pending_cover_sheet", False)),
            "signed_uploaded": bool(getattr(user, "signed_cover_sheet_uploaded", False)),
            "vendor_url_missing": not bool(website),
            "pdpa": pdpa_payload,
            "rfp": rfp_payload,
            "cover_sheet": cs_payload,
            "signed": signed_payload,
        }
    finally:
        db.close()


@router.post("/cover-sheet/upload-signed")
async def upload_signed_cover_sheet(
    file: UploadFile = File(...),
    email: str = Form(...),
):
    """
    Upload the user's signed Compliance Cover Sheet PDF.
    Consumes one `compliance_evidence_credits`, anchors on-chain via celery,
    and triggers a cover sheet regeneration (final blockchain receipt email).
    """
    if not file.filename or not _validate_signed_extension(file.filename):
        raise HTTPException(status_code=400, detail="Signed Cover Sheet must be a PDF.")

    contents = await file.read()
    if len(contents) == 0:
        raise HTTPException(status_code=400, detail="File is empty.")
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File too large. Maximum 50 MB.")

    file_hash = hashlib.sha256(contents).hexdigest()

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")
        credits = getattr(user, "compliance_evidence_credits", 0) or 0
        if credits <= 0:
            raise HTTPException(
                status_code=403,
                detail="No Compliance Evidence credit available. This upload requires a Compliance Evidence Pack purchase.",
            )
        if getattr(user, "signed_cover_sheet_uploaded", False):
            raise HTTPException(
                status_code=400,
                detail="A signed Cover Sheet has already been uploaded for this account.",
            )

        # Stash the signed PDF in S3
        report_id = str(uuid.uuid4())
        s3_key = f"signed_cover_sheets/{report_id}/{file.filename}"
        try:
            s3 = S3Service()
            s3.s3_client.put_object(
                Bucket=s3.bucket,
                Key=s3_key,
                Body=contents,
                ContentType="application/pdf",
                Metadata={
                    "report-id": report_id,
                    "file-hash": file_hash,
                    "kind": "signed-cover-sheet",
                },
            )
        except Exception as e:
            logger.error(f"[SignedCS] S3 upload failed: {e}")
            raise HTTPException(status_code=500, detail="File upload failed.")

        # Persist report row + decrement credit + flip flag atomically
        report = Report(
            id=report_id,
            owner_id=user.id,
            framework="compliance_evidence_signed_sheet",
            company_name=(user.company or "Your Organisation"),
            assessment_data={
                "file_hash": file_hash,
                "hash_algorithm": "SHA-256",
                "original_filename": file.filename,
                "file_size_bytes": len(contents),
                "mime_type": "application/pdf",
                "s3_key": s3_key,
                "contact_email": email,
                "payment_confirmed": True,
                "compliance_evidence_credit_redeemed": True,
            },
            status="pending",
        )
        report.audit_hash = file_hash
        db.add(report)

        user.compliance_evidence_credits = max(0, credits - 1)
        user.signed_cover_sheet_uploaded = True
        user.pending_cover_sheet = False
        company_name = (user.company or "").strip() or "Your Organisation"
        db.commit()
        logger.info(
            f"[SignedCS] {email} uploaded signed cover sheet (report={report_id}), "
            f"CE credits {credits} → {user.compliance_evidence_credits}"
        )
    finally:
        db.close()

    # Queue the anchor + regen pipeline.
    from app.workers.tasks import anchor_signed_cover_sheet_task
    anchor_signed_cover_sheet_task.apply_async(
        kwargs={
            "report_id": report_id,
            "customer_email": email,
            "company_name": company_name,
        },
        countdown=5,
    )

    return {
        "report_id": report_id,
        "file_hash": file_hash,
        "credits_remaining": 0,
        "queued": True,
    }
