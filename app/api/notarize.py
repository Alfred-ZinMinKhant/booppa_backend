"""
Notarization API
=================
Public endpoints for document upload, SHA-256 hash computation,
and certificate status retrieval.
No authentication required for upload — payment is handled via Stripe afterwards.
Enterprise subscribers use included monthly credits (no Stripe checkout needed).
"""

from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, UploadFile, File, Form, BackgroundTasks
from app.core.db import SessionLocal
from app.core.models import Report, User
from app.core.config import settings
from app.services.storage import S3Service
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
    document_descriptor: Optional[str] = Form(None),
    regulation_tag: Optional[str] = Form(None),
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
        # Check for bundle-granted notarization credits on this email.
        # If the user has credits, skip Stripe and queue fulfillment immediately.
        bundle_credit_user = None
        if email:
            bundle_credit_user = db.query(User).filter(User.email == email).first()
            if bundle_credit_user and (getattr(bundle_credit_user, "notarization_credits", 0) or 0) > 0:
                logger.info(
                    f"[Notarize] User {email} has {bundle_credit_user.notarization_credits} bundle credits — "
                    f"redeeming one for upload"
                )
            else:
                bundle_credit_user = None

        # Derive MIME type: prefer explicit content_type, fallback to extension guess
        mime_type = file.content_type or "application/octet-stream"

        # Validate regulation_tag against known keys
        _valid_regulation_tags = {"PDPA", "ACRA", "GEBIZ", "MAS"}
        safe_regulation_tag = (regulation_tag or "").strip().upper() or None
        if safe_regulation_tag and safe_regulation_tag not in _valid_regulation_tags:
            safe_regulation_tag = None

        assessment_data = {
            "file_hash": file_hash,
            "hash_algorithm": "SHA-256",
            "original_filename": file.filename,
            "file_size_bytes": len(contents),
            "mime_type": mime_type,
            "document_descriptor": (document_descriptor or "").strip()[:120] or None,
            "s3_key": s3_key,
            "plan": plan,
            "product_type": product_type,
            "notarization_type": "document",
            "regulation_tag": safe_regulation_tag,
        }
        if email:
            assessment_data["contact_email"] = email

        # If redeeming a bundle credit, mark as paid so fulfillment runs without Stripe.
        # Tag generically — credits are fungible across bundles, so we don't track
        # which bundle a specific document belongs to.
        if bundle_credit_user is not None:
            assessment_data["payment_confirmed"] = True
            assessment_data["bundle_credit_redeemed"] = True

        report = Report(
            id=report_id,
            owner_id=bundle_credit_user.id if bundle_credit_user else str(uuid.uuid4()),
            framework="compliance_notarization",
            company_name=company_name or "Notarization",
            assessment_data=assessment_data,
            status="pending",
        )
        report.audit_hash = file_hash
        db.add(report)

        # Decrement credit + queue fulfillment if redeeming
        credits_remaining = None
        if bundle_credit_user is not None:
            bundle_credit_user.notarization_credits = max(
                0, (bundle_credit_user.notarization_credits or 0) - 1
            )
            credits_remaining = bundle_credit_user.notarization_credits
            db.commit()
            from app.workers.tasks import fulfill_notarization_task
            fulfill_notarization_task.delay(report_id, email)
            logger.info(
                f"[Notarize] Redeemed bundle credit for {email}, queued fulfillment for {report_id}, "
                f"remaining credits={credits_remaining}"
            )
            # Auto-fire cover sheet only for users who bought a Compliance Evidence Pack
            # (the only bundle that includes one). Fires when they've used their last credit.
            # 60s countdown gives the notarization task time to anchor on-chain first.
            if credits_remaining == 0 and getattr(bundle_credit_user, "pending_cover_sheet", False):
                try:
                    from app.workers.tasks import fulfill_cover_sheet_task
                    fulfill_cover_sheet_task.apply_async(
                        kwargs={
                            "bundle_type": "compliance_evidence_pack",
                            "customer_email": email,
                            "company_name": company_name or (bundle_credit_user.company or ""),
                            "metadata": {},
                        },
                        countdown=60,
                    )
                    bundle_credit_user.pending_cover_sheet = False
                    db.commit()
                    logger.info(f"[Notarize] Last credit redeemed — auto-queued cover sheet for {email}")
                except Exception as cs_err:
                    logger.warning(f"[Notarize] Cover sheet auto-fire failed: {cs_err}")
        else:
            db.commit()

        response = {
            "report_id": report_id,
            "file_hash": file_hash,
            "file_name": file.filename,
            "file_size": len(contents),
            "product_type": product_type,
        }
        if bundle_credit_user is not None:
            response["bundle_credit_redeemed"] = True
            response["credits_remaining"] = credits_remaining
            response["skip_checkout"] = True
        return response
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to create notarization report: {e}")
        raise HTTPException(status_code=500, detail="Failed to process upload.")
    finally:
        db.close()


def _check_enterprise_credits(db, user_id: str, plan: str) -> dict:
    """
    Check if an enterprise user has notarization credits remaining this month.
    Returns {"has_credits": bool, "used": int, "limit": int, "month": str}.
    """
    from app.core.models_v8 import NotarizationCredit, ENTERPRISE_NOTARIZATION_LIMITS

    monthly_limit = ENTERPRISE_NOTARIZATION_LIMITS.get(plan)
    if monthly_limit is None:
        return {"has_credits": False, "used": 0, "limit": 0, "month": ""}

    current_month = datetime.now(timezone.utc).strftime("%Y-%m")

    credit = db.query(NotarizationCredit).filter(
        NotarizationCredit.user_id == user_id,
        NotarizationCredit.month == current_month,
    ).first()

    used = credit.used if credit else 0

    # -1 means unlimited
    if monthly_limit == -1:
        return {"has_credits": True, "used": used, "limit": -1, "month": current_month}

    return {
        "has_credits": used < monthly_limit,
        "used": used,
        "limit": monthly_limit,
        "month": current_month,
    }


def _consume_credit(db, user_id: str, plan: str) -> None:
    """Increment the used count for this user's current month. Creates row if needed."""
    from app.core.models_v8 import NotarizationCredit, ENTERPRISE_NOTARIZATION_LIMITS

    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    monthly_limit = ENTERPRISE_NOTARIZATION_LIMITS.get(plan, 5000)
    if monthly_limit == -1:
        monthly_limit = 999999  # store a large number for unlimited plans

    credit = db.query(NotarizationCredit).filter(
        NotarizationCredit.user_id == user_id,
        NotarizationCredit.month == current_month,
    ).first()

    if credit:
        credit.used += 1
        credit.updated_at = datetime.now(timezone.utc)
    else:
        credit = NotarizationCredit(
            user_id=user_id,
            month=current_month,
            used=1,
            monthly_limit=monthly_limit,
        )
        db.add(credit)
    db.flush()


@router.post("/enterprise/upload")
async def enterprise_upload_document(
    file: UploadFile = File(...),
    email: Optional[str] = Form(None),
    company_name: Optional[str] = Form(None),
    document_descriptor: Optional[str] = Form(None),
):
    """
    Enterprise notarization — uses monthly credits instead of Stripe.
    Requires a logged-in enterprise user (identified by email).
    Automatically triggers fulfillment (no checkout step).
    """
    if not email:
        raise HTTPException(status_code=400, detail="Email required for enterprise notarization.")

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

    db = SessionLocal()
    try:
        # Verify enterprise user
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")

        plan = getattr(user, "plan", "free") or "free"
        enterprise_plans = {"enterprise", "enterprise_pro", "standard_compliance", "pro_compliance"}
        if plan not in enterprise_plans:
            raise HTTPException(status_code=403, detail="Enterprise plan required for included notarizations.")

        # Check credits
        credit_info = _check_enterprise_credits(db, str(user.id), plan)
        if not credit_info["has_credits"]:
            raise HTTPException(
                status_code=429,
                detail=f"Monthly notarization limit reached ({credit_info['limit']} / month). "
                       f"Purchase additional notarizations or upgrade to Enterprise Pro for unlimited.",
            )

        # Compute hash + upload to S3
        file_hash = hashlib.sha256(contents).hexdigest()
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
            logger.error(f"S3 upload failed for enterprise notarization: {e}")
            raise HTTPException(status_code=500, detail="File upload failed. Please try again.")

        mime_type = file.content_type or "application/octet-stream"

        # Create report (already paid via credits)
        assessment_data = {
            "file_hash": file_hash,
            "hash_algorithm": "SHA-256",
            "original_filename": file.filename,
            "file_size_bytes": len(contents),
            "mime_type": mime_type,
            "document_descriptor": (document_descriptor or "").strip()[:120] or None,
            "s3_key": s3_key,
            "plan": "enterprise_credit",
            "product_type": "compliance_notarization_1",
            "notarization_type": "document",
            "payment_confirmed": True,
            "enterprise_credit": True,
            "contact_email": email,
        }

        report = Report(
            id=report_id,
            owner_id=user.id,
            framework="compliance_notarization",
            company_name=company_name or user.company or "Enterprise Notarization",
            assessment_data=assessment_data,
            status="pending",
        )
        report.audit_hash = file_hash
        db.add(report)

        # Consume credit
        _consume_credit(db, str(user.id), plan)

        db.commit()

        # Trigger fulfillment immediately (no Stripe checkout step)
        from app.workers.tasks import fulfill_notarization_task
        fulfill_notarization_task.delay(str(report.id), email)
        logger.info(f"Enterprise notarization queued for {email}, report={report_id}, credits used={credit_info['used'] + 1}")

        return {
            "report_id": report_id,
            "file_hash": file_hash,
            "file_name": file.filename,
            "file_size": len(contents),
            "product_type": "compliance_notarization_1",
            "enterprise_credit": True,
            "credits_used": credit_info["used"] + 1,
            "credits_limit": credit_info["limit"],
            "credits_remaining": (credit_info["limit"] - credit_info["used"] - 1) if credit_info["limit"] != -1 else -1,
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Enterprise notarization failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to process upload.")
    finally:
        db.close()


@router.get("/credits")
async def get_bundle_credits(email: str):
    """Check bundle-granted notarization credit balance + cover-sheet eligibility."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            return {"balance": 0, "has_credits": False, "pending_cover_sheet": False}
        balance = getattr(user, "notarization_credits", 0) or 0
        return {
            "balance": balance,
            "has_credits": balance > 0,
            "pending_cover_sheet": bool(getattr(user, "pending_cover_sheet", False)),
        }
    finally:
        db.close()


@router.get("/bundle/notarizations")
async def list_bundle_notarizations(email: str):
    """
    List all bundle-credit-redeemed notarization reports for an email
    (regardless of which bundle granted the credit). Used by the upload page.
    """
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            return {"reports": []}
        rows = (
            db.query(Report)
            .filter(
                Report.owner_id == user.id,
                Report.framework == "compliance_notarization",
            )
            .order_by(Report.created_at.desc())
            .limit(50)
            .all()
        )
        reports = []
        for r in rows:
            ad = r.assessment_data if isinstance(r.assessment_data, dict) else {}
            if not ad.get("bundle_credit_redeemed"):
                continue
            reports.append({
                "report_id": str(r.id),
                "file_name": ad.get("original_filename"),
                "file_hash": ad.get("file_hash") or r.audit_hash,
                "tx_hash": r.tx_hash,
                "status": r.status,
                "anchored_at": ad.get("blockchain_anchored_at"),
                "document_descriptor": ad.get("document_descriptor"),
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })
        return {"reports": reports, "count": len(reports)}
    finally:
        db.close()


@router.get("/bundle/cover-sheet/status")
async def bundle_cover_sheet_status(email: str):
    """
    Status of the Compliance Evidence Pack cover sheet for a user.
    Returns:
      - cover_sheet: { ready, generated_at, download_url }
      - pdpa: { status, score, completed_at }
      - vendor_proof: { status, completed_at }
      - notarizations: { anchored, total }
    """
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            return {"cover_sheet": {"ready": False}, "pdpa": None, "vendor_proof": None}

        cs = (
            db.query(Report)
            .filter(Report.owner_id == user.id, Report.framework == "compliance_evidence_pack")
            .order_by(Report.created_at.desc())
            .first()
        )
        cover_sheet_payload: dict = {"ready": False, "pending": bool(getattr(user, "pending_cover_sheet", False))}
        if cs:
            download_url = cs.s3_url
            if cs.file_key:
                try:
                    from app.services.storage import S3Service
                    s3 = S3Service()
                    download_url = s3.s3_client.generate_presigned_url(
                        "get_object",
                        Params={"Bucket": s3.bucket, "Key": cs.file_key},
                        ExpiresIn=604800,
                    )
                except Exception as e:
                    logger.warning(f"[CoverSheetStatus] presign failed: {e}")
            cover_sheet_payload = {
                "ready": cs.status == "completed",
                "pending": False,
                "generated_at": cs.completed_at.isoformat() if cs.completed_at else None,
                "download_url": download_url,
                "tx_hash": cs.tx_hash,
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
            ad = pdpa.assessment_data if isinstance(pdpa.assessment_data, dict) else {}
            raw_risk = (
                ad.get("overall_risk_score")
                if ad.get("overall_risk_score") is not None
                else ad.get("score")
                if ad.get("score") is not None
                else ad.get("risk_score")
            )
            score_val = None
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

        vp = (
            db.query(Report)
            .filter(Report.owner_id == user.id, Report.framework == "vendor_proof")
            .order_by(Report.created_at.desc())
            .first()
        )
        vp_payload = None
        if vp:
            vp_payload = {
                "status": vp.status,
                "completed_at": vp.completed_at.isoformat() if vp.completed_at else None,
            }

        anchored = (
            db.query(Report)
            .filter(Report.owner_id == user.id, Report.framework == "compliance_notarization")
            .all()
        )
        anchored_total = sum(1 for r in anchored if isinstance(r.assessment_data, dict) and r.assessment_data.get("bundle_credit_redeemed"))
        anchored_done = sum(1 for r in anchored if isinstance(r.assessment_data, dict) and r.assessment_data.get("bundle_credit_redeemed") and r.tx_hash)

        return {
            "cover_sheet": cover_sheet_payload,
            "pdpa": pdpa_payload,
            "vendor_proof": vp_payload,
            "notarizations": {"anchored": anchored_done, "total": anchored_total},
        }
    finally:
        db.close()


@router.post("/bundle/cover-sheet/trigger")
async def trigger_bundle_cover_sheet(payload: dict):
    """
    Manually fire the cover sheet for a Compliance Evidence Pack purchase.
    Body: {"email": "...", "company_name": "..." (optional)}
    Cover sheet will include all bundle-redeemed notarizations as anchored evidence.
    """
    email = (payload.get("email") or "").strip().lower()
    company_name = (payload.get("company_name") or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="email is required")

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if not getattr(user, "pending_cover_sheet", False):
            raise HTTPException(
                status_code=400,
                detail="Cover sheet only available for Compliance Evidence Pack purchases. "
                       "If you bought one and your cover sheet has already been generated, check your email.",
            )
        if not company_name:
            company_name = (user.company or "").strip() or "Your Organisation"
        # Clear flag now so duplicate clicks don't re-fire
        user.pending_cover_sheet = False
        db.commit()
    finally:
        db.close()

    from app.workers.tasks import fulfill_cover_sheet_task
    fulfill_cover_sheet_task.apply_async(
        kwargs={
            "bundle_type": "compliance_evidence_pack",
            "customer_email": email,
            "company_name": company_name,
            "metadata": {"user_triggered": True},
        },
        countdown=10,
    )
    logger.info(f"[Bundle] User-triggered cover sheet for {email}")
    return {"queued": True, "email": email}


@router.get("/enterprise/credits")
async def get_enterprise_credits(email: str):
    """Check remaining notarization credits for an enterprise user."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")

        plan = getattr(user, "plan", "free") or "free"
        enterprise_plans = {"enterprise", "enterprise_pro", "standard_compliance", "pro_compliance"}
        if plan not in enterprise_plans:
            return {"has_credits": False, "used": 0, "limit": 0, "plan": plan, "enterprise": False}

        credit_info = _check_enterprise_credits(db, str(user.id), plan)
        return {
            **credit_info,
            "plan": plan,
            "enterprise": True,
            "remaining": (credit_info["limit"] - credit_info["used"]) if credit_info["limit"] != -1 else -1,
        }
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
async def get_certificate(report_id: str, session_id: str | None = None, background_tasks: BackgroundTasks = None):
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

        # Self-heal: if fulfillment never ran, trigger it once.
        # Primary path: webhook already set payment_confirmed=True.
        # Fallback path: webhook may have been delayed/missed — verify payment
        # directly with Stripe using the session_id passed by the frontend poller.
        payment_confirmed_flag = bool(assessment.get("payment_confirmed"))

        if (
            not payment_confirmed_flag
            and session_id
            and report.status in {"pending", "processing"}
            and not assessment.get("pdf_generated")
            and not assessment.get("fulfillment_triggered")
            and background_tasks is not None
        ):
            # Verify with Stripe directly (webhook may not have arrived yet)
            try:
                import stripe as _stripe
                from app.core.config import settings as _settings
                _stripe.api_key = _settings.STRIPE_SECRET_KEY
                _session = _stripe.checkout.Session.retrieve(session_id)
                _pstatus = getattr(_session, "payment_status", None) or (
                    _session.get("payment_status") if hasattr(_session, "get") else None
                )
                if _pstatus == "paid":
                    payment_confirmed_flag = True
                    assessment["payment_confirmed"] = True
                    _meta = getattr(_session, "metadata", None) or {}
                    if not assessment.get("contact_email") and _meta.get("customer_email"):
                        assessment["contact_email"] = _meta["customer_email"]
                    report.assessment_data = assessment
                    from sqlalchemy.orm.attributes import flag_modified
                    flag_modified(report, "assessment_data")
                    db.commit()
                    logger.info(f"[Notarize] Stripe-verified payment for {report_id}, setting payment_confirmed")
            except Exception as _ve:
                logger.warning(f"[Notarize] Stripe session verify failed for {report_id}: {_ve}")

        if (
            payment_confirmed_flag
            and report.status in {"pending", "processing"}
            and not assessment.get("pdf_generated")
            and not assessment.get("fulfillment_triggered")
            and background_tasks is not None
        ):
            assessment["fulfillment_triggered"] = True
            report.assessment_data = assessment
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(report, "assessment_data")
            db.commit()
            from app.workers.tasks import fulfill_notarization_task
            contact_email = assessment.get("contact_email") or assessment.get("customer_email")
            fulfill_notarization_task.delay(str(report.id), contact_email)
            logger.info(f"[Notarize] Self-heal triggered for stuck report {report_id}")

        # Pipeline step flags
        payment_confirmed = bool(assessment.get("payment_confirmed"))
        pdf_generated = bool(assessment.get("pdf_generated"))
        s3_uploaded = bool(assessment.get("s3_uploaded"))
        # Only treat a real hex tx hash as valid (not the legacy "already_anchored" sentinel)
        real_tx = report.tx_hash if (report.tx_hash and report.tx_hash != "already_anchored") else None
        has_tx = bool(assessment.get("blockchain_anchored"))

        # Verification payload
        verification = None
        if report.audit_hash:
            verify_url = f"{settings.VERIFY_BASE_URL.rstrip('/')}/verify/{report.audit_hash}"
            polygonscan_url = f"{settings.POLYGON_EXPLORER_URL.rstrip('/')}/tx/{real_tx}" if real_tx else None

            anchored = bool(assessment.get("blockchain_anchored", False))
            anchored_at = assessment.get("blockchain_anchored_at")

            verification = {
                "verify_url": verify_url,
                "polygonscan_url": polygonscan_url,
                "qr_image": _build_qr_base64(verify_url),
                "proof_header": "BOOPPA-PROOF-SG",
                "schema_version": "1.0",
                "anchored": anchored,
                "anchored_at": anchored_at,
                "network": settings.POLYGON_NETWORK_NAME,
                "testnet_notice": settings.POLYGON_TESTNET_NOTICE,
            }

        return {
            "status": report.status,
            "report_id": str(report.id),
            # Document info
            "document_descriptor": assessment.get("document_descriptor"),
            "file_name": assessment.get("original_filename"),
            "file_hash": assessment.get("file_hash"),
            "hash_algorithm": assessment.get("hash_algorithm", "SHA-256"),
            "file_size": assessment.get("file_size_bytes"),
            "mime_type": assessment.get("mime_type"),
            "company_name": report.company_name,
            # Blockchain & evidence
            "audit_hash": report.audit_hash,
            "tx_hash": real_tx,
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
