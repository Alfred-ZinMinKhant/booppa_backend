from app.core.route_classes import RetryAPIRoute
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
from fastapi import APIRouter, HTTPException, Request, Response, UploadFile, File, Form
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from typing import Optional
import hashlib
import logging
import uuid

from app.core.db import SessionLocal
from app.core.models import Report, User
from app.core.repositories.report_repository import ReportRepository
from app.core.repositories.user_repository import UserRepository
from app.services.storage import S3Service

logger = logging.getLogger(__name__)
router = APIRouter(route_class=RetryAPIRoute)

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


@router.get("/cover-sheet/download/{report_id}")
async def download_cover_sheet(report_id: str):
    """
    Stable redirect to a fresh presigned URL for a generated cover sheet PDF.
    Used in the "Cover Sheet ready" email so the link does not go 403 once
    the original presigned URL (or its STS-signing credentials) expire. The
    report_id is a UUID — unguessable enough to serve as the access token
    for the recipient's own document.
    """
    db = SessionLocal()
    try:
        report = ReportRepository.get_by_id_and_framework(db, report_id, "compliance_evidence_pack")
        if not report:
            raise HTTPException(status_code=404, detail="Cover sheet not found.")
        ad = report.assessment_data if isinstance(report.assessment_data, dict) else {}
        key = report.file_key or ad.get("s3_key")
        if not key:
            raise HTTPException(status_code=404, detail="Cover sheet PDF not available.")
        url = _presign(key)
        if not url:
            raise HTTPException(status_code=500, detail="Could not generate download URL.")
        return RedirectResponse(url=url, status_code=302)
    finally:
        db.close()


@router.get("/cover-sheet/status")
async def cover_sheet_status(email: str, response: Response):
    """
    One-stop status feed for the dedicated /compliance/cover-sheet page.
    Includes: pdpa, rfp, cover_sheet (issued PDF), signed (uploaded copy),
    credits balance, and the pending_cover_sheet flag.

    Cache-Control: no-store — the buyer polls this every 8s while the brief
    state flips ("pending" → "submitted") on the webhook + intake submit.
    Without no-store, Cloudflare can serve a stale pre-submit body back to
    the page and the "Complete brief" CTA stays stuck after the user has
    actually submitted.
    """
    response.headers["Cache-Control"] = "no-store"
    db = SessionLocal()
    try:
        user = UserRepository.get_by_email(db, email)
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

        pdpa = ReportRepository.get_latest_for_owner_by_frameworks(db, user.id, ["pdpa_quick_scan", "pdpa_snapshot"])
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

        rfp = ReportRepository.get_latest_for_owner_by_framework(db, user.id, "rfp_complete")
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
        else:
            # Backfill: some RFP completions predate the unconditional Report-row
            # write. The CertificateLog row is always written, so use it as a
            # fallback so the bundle progress page can show "completed".
            try:
                from app.core.models import CertificateLog
                cert = (
                    db.query(CertificateLog)
                    .filter(
                        CertificateLog.vendor_id == user.id,
                        CertificateLog.certificate_type == "RFP",
                    )
                    .order_by(CertificateLog.generated_at.desc())
                    .first()
                )
                if cert:
                    # The legacy _write_certificate_log hardcoded file_key to
                    # `rfp-express/{report_id}.pdf` regardless of tier and
                    # without the `reports/` prefix used by S3Service. The
                    # bundle flow always uses rfp_complete, so try the
                    # canonical key first; fall back to the stored value.
                    canonical_key = (
                        f"reports/rfp-complete/{cert.report_id}.pdf"
                        if cert.report_id
                        else None
                    )
                    rfp_payload = {
                        "status": "completed",
                        "completed_at": cert.generated_at.isoformat() if cert.generated_at else None,
                        "download_url": _presign(canonical_key) or _presign(cert.file_key),
                    }
            except Exception as e:
                # Roll back the failed transaction so subsequent queries on this
                # session don't fail with `InFailedSqlTransaction`. The most
                # common cause here is a schema/migration mismatch on
                # certificate_logs in some environments.
                logger.warning(f"[ComplianceStatus] RFP CertificateLog fallback failed: {e}")
                try:
                    db.rollback()
                except Exception:
                    pass

        cs = ReportRepository.get_latest_for_owner_by_framework(db, user.id, "compliance_evidence_pack")
        cs_payload = {"ready": False}
        if cs and (cs.file_key or cs.s3_url):
            from app.services.cover_sheet_generator import COVER_SHEET_SCHEMA_VERSION
            cs_ad = cs.assessment_data if isinstance(cs.assessment_data, dict) else {}
            cs_key = cs.file_key or cs_ad.get("s3_key")
            stored_version = cs_ad.get("schema_version")
            cs_payload = {
                "ready": True,
                "download_url": _presign(cs_key) or cs.s3_url,
                "tx_hash": cs.tx_hash,
                "generated_at": cs.completed_at.isoformat() if cs.completed_at else None,
                "schema_version": stored_version,
                "outdated": stored_version != COVER_SHEET_SCHEMA_VERSION,
            }
            # The cover sheet only generates after both PDPA and RFP finish, so
            # its existence is proof those inputs completed at the time it was
            # issued. Only fall back to the cover-sheet snapshot when the
            # upstream Report row is missing entirely AND any row that exists
            # is older than the cover sheet. A newer pending row means a fresh
            # purchase cycle is in flight — show that real state, not the
            # stale snapshot.
            cs_generated_at = cs.completed_at
            pdpa_is_newer_cycle = (
                pdpa is not None
                and cs_generated_at is not None
                and pdpa.created_at is not None
                and pdpa.created_at > cs_generated_at
            )
            rfp_is_newer_cycle = (
                rfp is not None
                and cs_generated_at is not None
                and rfp.created_at is not None
                and rfp.created_at > cs_generated_at
            )
            if pdpa_payload is None and not pdpa_is_newer_cycle:
                snapshot_score = cs_ad.get("pdpa_score")
                pdpa_payload = {
                    "status": "completed",
                    "score": snapshot_score if isinstance(snapshot_score, int) else None,
                    "completed_at": cs_payload["generated_at"],
                }
            if rfp_payload is None and not rfp_is_newer_cycle:
                rfp_payload = {
                    "status": "completed",
                    "completed_at": cs_payload["generated_at"],
                    "download_url": cs_ad.get("rfp_download_url"),
                }
            # Mark the cover sheet itself as not-yet-current when a fresh
            # cycle is running, so the UI doesn't show old PDF + new tiles.
            if pdpa_is_newer_cycle or rfp_is_newer_cycle:
                cs_payload["stale"] = True

        # Scope to current cycle: only show signed report from after the latest
        # PDPA scan (so monthly subscribers don't see last month's signed sheet
        # bleed into this cycle's UI).
        # Keep inline query for conditional timestamp filter since it's highly specific
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
            # Surface the signature method + signer identity captured at
            # sign-time so the UI can render a proper "signed receipt"
            # panel rather than just file + tx. Electronic signatures
            # carry signature_method='electronic' from the e-sign endpoint;
            # wet-sign uploads have no such key (rendered as 'uploaded').
            signed_payload = {
                "uploaded_at": signed.created_at.isoformat() if signed.created_at else None,
                "tx_hash": signed.tx_hash,
                "file_hash": s_ad.get("file_hash") or signed.audit_hash,
                "file_name": s_ad.get("original_filename"),
                "signature_method": s_ad.get("signature_method") or "uploaded",
                "signer_name": s_ad.get("signer_name"),
                "signer_title": s_ad.get("signer_title"),
                "signed_at_utc": s_ad.get("signed_at_utc"),
                "legal_basis": s_ad.get("legal_basis"),
                # Surface the anchor-failure flag so the UI can stop the
                # "Anchoring on-chain..." spinner and show a clear recovery
                # CTA instead of leaving the buyer wondering.
                "signed_report_id": str(signed.id),
                "anchor_failed": bool(s_ad.get("anchor_failed")),
                "anchor_failed_at": s_ad.get("anchor_failed_at"),
                "anchor_failed_reason": s_ad.get("anchor_failed_reason"),
            }

        # Surface the brief CTA only if the buyer's CURRENT Compliance Bundle
        # intake is still pending. Take the latest CE-pack intake regardless of
        # status, then check it — this way an old pending row from a prior
        # purchase doesn't keep the CTA stuck after the buyer has actually
        # submitted today's bundle's brief. (Don't change to "latest pending"
        # — that's the regression pattern called out in CLAUDE.md for the
        # /stripe/checkout/verify endpoint, and the same trap applies here.)
        rfp_brief_intake_id = None
        try:
            from app.core.models import PendingRfpIntake
            latest_ce_intake = (
                db.query(PendingRfpIntake)
                .filter(
                    PendingRfpIntake.user_id == user.id,
                    PendingRfpIntake.bundle_source == "compliance_evidence_pack",
                )
                .order_by(PendingRfpIntake.created_at.desc())
                .first()
            )
            if latest_ce_intake and latest_ce_intake.status == "pending":
                rfp_brief_intake_id = str(latest_ce_intake.id)
        except Exception as e:
            logger.warning(f"[ComplianceStatus] PendingRfpIntake lookup failed: {e}")

        website = (getattr(user, "website", "") or "").strip()
        return {
            "credits": getattr(user, "compliance_evidence_credits", 0) or 0,
            "pending_cover_sheet": bool(getattr(user, "pending_cover_sheet", False)),
            "signed_uploaded": bool(getattr(user, "signed_cover_sheet_uploaded", False)),
            "vendor_url_missing": not bool(website),
            "pdpa": pdpa_payload,
            "rfp": rfp_payload,
            "rfp_brief_intake_id": rfp_brief_intake_id,
            "cover_sheet": cs_payload,
            "signed": signed_payload,
        }
    finally:
        db.close()


@router.post("/cover-sheet/regenerate")
async def regenerate_cover_sheet(email: str = Form(...)):
    """
    Run fulfill_cover_sheet_task inline with force=True for users whose
    existing cover sheet was issued by a previous version of
    cover_sheet_generator.py. Runs synchronously so any failure surfaces
    directly in the API response instead of getting buried in celery retries.
    """
    db = SessionLocal()
    try:
        user = UserRepository.get_by_email(db, email)
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")
        existing = ReportRepository.get_latest_for_owner_by_framework(db, user.id, "compliance_evidence_pack")
        if not existing:
            raise HTTPException(
                status_code=400,
                detail="No existing Compliance Cover Sheet found for this account.",
            )
        company_name = (user.company or "").strip() or "Your Organisation"
    finally:
        db.close()

    # Run the task body inline via celery's .apply() (sync). The async celery
    # task wrapper retries silently on failure, which buries the real error;
    # apply() runs in-process and propagates exceptions to us.
    from app.workers.tasks import fulfill_cover_sheet_task

    result = fulfill_cover_sheet_task.apply(
        kwargs={
            "bundle_type": "compliance_evidence_pack",
            "customer_email": email,
            "company_name": company_name,
            "metadata": {"force": True, "regen_manual": True},
        },
        throw=False,
    )
    if result.failed():
        exc = result.result
        logger.exception(f"[CoverSheet] Inline regen failed for {email}: {exc}")
        raise HTTPException(status_code=500, detail=f"Regeneration failed: {exc}")

    logger.info(f"[CoverSheet] Inline regen complete for {email}")
    return {"queued": True, "inline": True}


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
        # Row-level lock — two concurrent signed-sheet uploads must not both
        # pass the credits/flag checks and double-anchor a single credit. The
        # loser blocks until the winner commits, then sees the cleared flag.
        user = UserRepository.get_by_email(db, email, lock_for_update=True)
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


class ESignAttestations(BaseModel):
    authorised: bool
    accurate: bool


class ESignRequest(BaseModel):
    email: str
    signer_name: str = Field(..., min_length=2, max_length=200)
    signer_title: str = Field(..., min_length=2, max_length=200)
    attestations: ESignAttestations


@router.post("/cover-sheet/sign-electronically")
async def sign_cover_sheet_electronically(payload: ESignRequest, request: Request):
    """
    In-browser electronic signature path for the Compliance Cover Sheet.

    Fetches the buyer's unsigned cover sheet from S3, appends a Signature Page
    capturing the typed signature + attestations + originating IP + UTC
    timestamp, and reuses the same downstream pipeline as the wet-signature
    upload (`anchor_signed_cover_sheet_task`). Same credit/lock semantics —
    consumes one `compliance_evidence_credits`, sets `signed_cover_sheet_uploaded`.

    Legal basis: Singapore Electronic Transactions Act (Cap. 88) s. 8 —
    typed-name + intention + binding to document hash satisfies the
    electronic-signature requirement for commercial purposes.
    """
    if not (payload.attestations.authorised and payload.attestations.accurate):
        raise HTTPException(
            status_code=400,
            detail="Both attestations must be ticked before submitting.",
        )

    signer_ip = (request.client.host if request.client else None) or request.headers.get(
        "x-forwarded-for", ""
    ).split(",")[0].strip() or None

    db = SessionLocal()
    try:
        # Row-level lock mirrors upload_signed_cover_sheet — two concurrent
        # sign-electronically calls must not both pass the credit check.
        user = UserRepository.get_by_email(db, payload.email, lock_for_update=True)
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")
        credits = getattr(user, "compliance_evidence_credits", 0) or 0
        if credits <= 0:
            raise HTTPException(
                status_code=403,
                detail="No Compliance Evidence credit available. This signature requires a Compliance Evidence Pack purchase.",
            )
        if getattr(user, "signed_cover_sheet_uploaded", False):
            raise HTTPException(
                status_code=400,
                detail="A signed Cover Sheet has already been recorded for this account.",
            )

        # Find the latest unsigned Cover Sheet (framework='compliance_evidence_pack')
        # for this user — that's the PDF we sign.
        unsigned_report = (
            db.query(Report)
            .filter(
                Report.owner_id == user.id,
                Report.framework == "compliance_evidence_pack",
                Report.status == "completed",
            )
            .order_by(Report.created_at.desc())
            .first()
        )
        if not unsigned_report:
            raise HTTPException(
                status_code=409,
                detail="No completed Cover Sheet found yet. Wait for the unsigned PDF to generate before signing.",
            )
        ad = unsigned_report.assessment_data or {}
        unsigned_s3_key = ad.get("s3_key")
        if not unsigned_s3_key:
            raise HTTPException(
                status_code=500,
                detail="Cover Sheet record is missing its S3 location. Contact support.",
            )

        # Fetch the unsigned PDF bytes
        try:
            s3 = S3Service()
            obj = s3.s3_client.get_object(Bucket=s3.bucket, Key=unsigned_s3_key)
            unsigned_pdf_bytes = obj["Body"].read()
        except Exception as e:
            logger.error(f"[ESignCS] S3 fetch failed for {unsigned_s3_key}: {e}")
            raise HTTPException(status_code=500, detail="Could not retrieve the unsigned Cover Sheet.")

        unsigned_sha256 = hashlib.sha256(unsigned_pdf_bytes).hexdigest()

        # Append the Signature Page
        try:
            from app.services.cover_sheet_generator import append_signature_page
            signed_pdf_bytes = append_signature_page(
                unsigned_pdf_bytes,
                signer_name=payload.signer_name.strip(),
                signer_title=payload.signer_title.strip(),
                signer_email=payload.email,
                company_name=(user.company or "Your Organisation"),
                signer_ip=signer_ip,
                unsigned_pdf_sha256=unsigned_sha256,
                attestations={
                    "authorised": payload.attestations.authorised,
                    "accurate": payload.attestations.accurate,
                },
            )
        except Exception as e:
            logger.error(f"[ESignCS] Signature page append failed: {e}")
            raise HTTPException(status_code=500, detail="Could not append the signature page.")

        signed_sha256 = hashlib.sha256(signed_pdf_bytes).hexdigest()

        # Stash the signed PDF in S3 — same path pattern as the upload route.
        report_id = str(uuid.uuid4())
        s3_key = f"signed_cover_sheets/{report_id}/cover-sheet-signed-electronically.pdf"
        try:
            s3.s3_client.put_object(
                Bucket=s3.bucket,
                Key=s3_key,
                Body=signed_pdf_bytes,
                ContentType="application/pdf",
                Metadata={
                    "report-id": report_id,
                    "file-hash": signed_sha256,
                    "kind": "signed-cover-sheet",
                    "signature-method": "electronic",
                },
            )
        except Exception as e:
            logger.error(f"[ESignCS] S3 upload failed: {e}")
            raise HTTPException(status_code=500, detail="Signed PDF upload failed.")

        # Persist Report row + decrement credit + flip flag atomically
        now_iso = datetime.now(timezone.utc).isoformat()
        report = Report(
            id=report_id,
            owner_id=user.id,
            framework="compliance_evidence_signed_sheet",
            company_name=(user.company or "Your Organisation"),
            assessment_data={
                "file_hash": signed_sha256,
                "hash_algorithm": "SHA-256",
                "original_filename": "cover-sheet-signed-electronically.pdf",
                "file_size_bytes": len(signed_pdf_bytes),
                "mime_type": "application/pdf",
                "s3_key": s3_key,
                "contact_email": payload.email,
                "payment_confirmed": True,
                "compliance_evidence_credit_redeemed": True,
                "signature_method": "electronic",
                "signer_name": payload.signer_name.strip(),
                "signer_title": payload.signer_title.strip(),
                "signer_ip": signer_ip,
                "signed_at_utc": now_iso,
                "attestations": {
                    "authorised": payload.attestations.authorised,
                    "accurate": payload.attestations.accurate,
                },
                "unsigned_pdf_sha256": unsigned_sha256,
                "unsigned_pdf_s3_key": unsigned_s3_key,
                "legal_basis": "Singapore Electronic Transactions Act (Cap. 88) s. 8",
            },
            status="pending",
        )
        report.audit_hash = signed_sha256
        db.add(report)

        user.compliance_evidence_credits = max(0, credits - 1)
        user.signed_cover_sheet_uploaded = True
        user.pending_cover_sheet = False
        company_name = (user.company or "").strip() or "Your Organisation"
        db.commit()
        logger.info(
            f"[ESignCS] {payload.email} signed cover sheet electronically (report={report_id}), "
            f"CE credits {credits} → {user.compliance_evidence_credits}"
        )
    finally:
        db.close()

    # Queue the anchor + regen pipeline — same task as the upload path.
    from app.workers.tasks import anchor_signed_cover_sheet_task
    anchor_signed_cover_sheet_task.apply_async(
        kwargs={
            "report_id": report_id,
            "customer_email": payload.email,
            "company_name": company_name,
        },
        countdown=5,
    )

    return {
        "report_id": report_id,
        "file_hash": signed_sha256,
        "credits_remaining": 0,
        "signature_method": "electronic",
        "queued": True,
    }
