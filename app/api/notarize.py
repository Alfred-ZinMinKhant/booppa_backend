"""
Notarization API
=================
Public endpoints for document upload, SHA-256 hash computation,
and certificate status retrieval.
No authentication required — payment is handled via Stripe afterwards.
"""

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from app.core.db import SessionLocal
from app.core.models import Report
from app.core.config import settings
from app.services.storage import S3Service
from app.services.blockchain import BlockchainService
import hashlib
import uuid
import base64
import logging
from io import BytesIO
from typing import Optional

try:
    import qrcode
except ImportError:
    qrcode = None

logger = logging.getLogger(__name__)
router = APIRouter()

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".doc", ".png", ".jpg", ".jpeg", ".txt", ".csv", ".xlsx"}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


def _validate_extension(filename: str) -> bool:
    lower = filename.lower()
    return any(lower.endswith(ext) for ext in ALLOWED_EXTENSIONS)


@router.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    email: Optional[str] = Form(None),
    company_name: Optional[str] = Form(None),
    plan: str = Form("single"),
):
    """
    Upload a document for notarization.

    1. Validates file type and size
    2. Computes SHA-256 hash of contents
    3. Uploads file to S3
    4. Creates a pending report record
    5. Returns report_id + file_hash for the checkout step
    """
    if not file.filename or not _validate_extension(file.filename):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    contents = await file.read()

    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File too large. Maximum 50 MB.")

    if len(contents) == 0:
        raise HTTPException(status_code=400, detail="File is empty.")

    # Compute SHA-256 hash of file contents
    file_hash = hashlib.sha256(contents).hexdigest()

    # Map plan to product type
    product_map = {
        "single": "compliance_notarization_1",
        "batch10": "compliance_notarization_10",
        "batch50": "compliance_notarization_50",
    }
    product_type = product_map.get(plan, "compliance_notarization_1")

    # Upload original file to S3
    report_id = str(uuid.uuid4())
    s3_key = f"notarization/{report_id}/{file.filename}"
    try:
        s3 = S3Service()
        s3.s3_client.put_object(
            Bucket=s3.bucket,
            Key=s3_key,
            Body=contents,
            ContentType=file.content_type or "application/octet-stream",
            Metadata={
                "report-id": report_id,
                "file-hash": file_hash,
                "original-filename": file.filename,
            },
        )
    except Exception as e:
        logger.error(f"S3 upload failed for notarization: {e}")
        raise HTTPException(status_code=500, detail="File upload failed. Please try again.")

    # Create a pending report record
    db = SessionLocal()
    try:
        assessment_data = {
            "file_hash": file_hash,
            "original_filename": file.filename,
            "file_size_bytes": len(contents),
            "s3_key": s3_key,
            "plan": plan,
            "product_type": product_type,
            "notarization_type": "document",
        }
        if email:
            assessment_data["contact_email"] = email

        report = Report(
            id=report_id,
            owner_id=str(uuid.uuid4()),
            framework="compliance_notarization",
            company_name=company_name or "Notarization",
            assessment_data=assessment_data,
            status="pending",
        )
        report.audit_hash = file_hash
        db.add(report)
        db.commit()

        return {
            "report_id": report_id,
            "file_hash": file_hash,
            "file_name": file.filename,
            "file_size": len(contents),
            "product_type": product_type,
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to create notarization report: {e}")
        raise HTTPException(status_code=500, detail="Failed to process upload.")
    finally:
        db.close()


def _build_qr_base64(target: str) -> str | None:
    """Generate a QR code PNG as a base64 string."""
    if not target or qrcode is None:
        return None
    try:
        qr = qrcode.QRCode(version=1, box_size=4, border=2)
        qr.add_data(target)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as exc:
        logger.warning("QR generation failed: %s", exc)
        return None


@router.get("/certificate/{report_id}")
async def get_certificate(report_id: str):
    """
    Public endpoint: return notarization certificate data for display on the frontend.
    Mirrors exactly what the PDF certificate contains.
    """
    db = SessionLocal()
    try:
        report = db.query(Report).filter(Report.id == report_id).first()
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")
        if report.framework != "compliance_notarization":
            raise HTTPException(status_code=404, detail="Not a notarization report")

        assessment = report.assessment_data if isinstance(report.assessment_data, dict) else {}

        # Pipeline step flags
        payment_confirmed = bool(assessment.get("payment_confirmed"))
        pdf_generated = bool(assessment.get("pdf_generated"))
        s3_uploaded = bool(assessment.get("s3_uploaded"))
        has_tx = bool(report.tx_hash)

        # Verification payload
        verification = None
        if report.audit_hash:
            verify_url = f"{settings.VERIFY_BASE_URL.rstrip('/')}/verify/{report.audit_hash}"
            polygonscan_url = f"{settings.POLYGON_EXPLORER_URL.rstrip('/')}/tx/{report.tx_hash}" if report.tx_hash else None

            anchored = False
            anchored_at = None
            if report.tx_hash:
                try:
                    blockchain = BlockchainService()
                    anchor_status = blockchain.get_anchor_status(report.audit_hash, tx_hash=report.tx_hash)
                    anchored = anchor_status.get("anchored", False)
                    anchored_at = anchor_status.get("anchored_at")
                except Exception as exc:
                    logger.warning("Blockchain status check failed: %s", exc)

            verification = {
                "verify_url": verify_url,
                "polygonscan_url": polygonscan_url,
                "qr_image": _build_qr_base64(verify_url),
                "proof_header": "BOOPPA-PROOF-SG",
                "schema_version": "1.0",
                "anchored": anchored,
                "anchored_at": anchored_at,
            }

        return {
            "status": report.status,
            "report_id": str(report.id),
            # Document info
            "file_name": assessment.get("original_filename"),
            "file_hash": assessment.get("file_hash"),
            "file_size": assessment.get("file_size_bytes"),
            "company_name": report.company_name,
            # Blockchain & evidence
            "audit_hash": report.audit_hash,
            "tx_hash": report.tx_hash,
            # Plan
            "plan": assessment.get("product_type"),
            "contact_email": assessment.get("contact_email"),
            # Timestamps
            "created_at": report.created_at.isoformat() if report.created_at else None,
            "completed_at": report.completed_at.isoformat() if report.completed_at else None,
            # PDF download
            "pdf_url": report.s3_url if s3_uploaded else None,
            # Pipeline progress
            "pipeline": {
                "payment_confirmed": payment_confirmed,
                "blockchain_anchored": has_tx,
                "pdf_generated": pdf_generated,
                "certificate_ready": report.status == "completed",
            },
            # Verification (QR, URLs, proof header)
            "verification": verification,
        }
    finally:
        db.close()
