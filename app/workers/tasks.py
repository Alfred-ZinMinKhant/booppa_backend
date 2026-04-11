from .celery_app import celery_app
from app.core.db import SessionLocal
from app.core.models import Report
from app.services.ai_service import AIService
from app.services.booppa_ai_service import BooppaAIService
from app.services.blockchain import BlockchainService
from app.services.pdf_service import PDFService
from app.services.storage import S3Service
from app.services.email_service import EmailService
from app.services.screenshot_service import capture_screenshot_base64
from app.core.config import settings
from app.billing.enforcement import enforce_tier
from app.services.audit_chain import append_audit_event
from app.services.dependency_logger import log_dependency_event
from app.services.verify_registry import register_verification
from app.integrations.ai.adapter import ai_preview
import asyncio
import hashlib
import json
import logging
import httpx
import base64
import re
from urllib.parse import urljoin
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def _set_assessment_values(report: Report, updates: dict) -> None:
    if not isinstance(updates, dict):
        return
    try:
        if isinstance(report.assessment_data, dict):
            assessment = dict(report.assessment_data)
        else:
            assessment = {}
        assessment.update(updates)
        report.assessment_data = assessment
    except Exception as e:
        logger.warning(f"Failed to update assessment_data for {report.id}: {e}")


async def _capture_screenshot_with_timeout(url: str, timeout: int = 25) -> str | None:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(capture_screenshot_base64, url), timeout=timeout
        )
    except asyncio.TimeoutError:
        logger.warning(f"Screenshot capture timed out for {url}")
        return None


async def _fetch_thum_io_base64(url: str, timeout: int = 20) -> tuple[str | None, str | None]:
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(f"https://image.thum.io/get/width/1400/{url}")
            if resp.status_code == 200 and resp.content:
                return base64.b64encode(resp.content).decode(), None
            return None, f"thum_io_status:{resp.status_code}"
    except Exception as e:
        return None, f"thum_io_error:{str(e)[:200]}"


async def _detect_cookie_banner(url: str | None) -> dict:
    if not url:
        return {}

    indicators = [
        "cookiebot",
        "usercentrics",
        "cookieyes",
        "onetrust",
        "osano",
        "iubenda",
        "cookie-consent",
        "cookie consent",
        "consentmanager",
        "data-cookieconsent",
    ]

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "BooppaComplianceBot/1.0"})
            if resp.status_code >= 400:
                return {"cookie_scan_error": f"status:{resp.status_code}"}
            html = resp.text.lower()
            found = [k for k in indicators if k in html]
            if found:
                return {
                    "consent_mechanism": {
                        "has_cookie_banner": True,
                        "has_active_consent": True,
                        "detected_providers": found,
                    }
                }
            return {"consent_mechanism": {"has_cookie_banner": False}}
    except Exception as e:
        return {"cookie_scan_error": f"error:{str(e)[:200]}"}


async def _scan_site_metadata(url: str | None) -> dict:
    if not url:
        return {}

    headers_result = {}
    page_result = {}
    html = ""

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "BooppaComplianceBot/1.0"})
            headers_result = {
                "hsts": bool(resp.headers.get("strict-transport-security")),
                "csp": bool(resp.headers.get("content-security-policy")),
                "x_content_type_options": bool(resp.headers.get("x-content-type-options")),
                "x_frame_options": bool(resp.headers.get("x-frame-options")),
                "referrer_policy": bool(resp.headers.get("referrer-policy")),
                "permissions_policy": bool(resp.headers.get("permissions-policy")),
            }
            html = resp.text or ""
    except Exception as e:
        page_result["scan_error"] = f"metadata_error:{str(e)[:200]}"

    html_lower = html.lower()
    combined_html = html_lower

    # Privacy policy detection
    privacy_link = None
    match = re.search(r'href=["\"]([^"\"]*privacy[^"\"]*)', html_lower)
    if match:
        privacy_link = match.group(1)
    page_result["privacy_policy"] = {
        "found": bool(privacy_link),
        "link": privacy_link,
    }

    # If privacy policy link is found, fetch it for deeper checks
    if privacy_link:
        try:
            privacy_url = (
                privacy_link
                if privacy_link.startswith("http")
                else urljoin(url, privacy_link)
            )
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                resp = await client.get(
                    privacy_url, headers={"User-Agent": "BooppaComplianceBot/1.0"}
                )
                if resp.status_code < 400:
                    combined_html += "\n" + (resp.text or "").lower()
        except Exception as e:
            page_result["privacy_policy_fetch_error"] = f"privacy_fetch:{str(e)[:200]}"

    # DPO detection
    has_dpo = "data protection officer" in combined_html or re.search(r"\bdpo\b", combined_html)
    dpo_email_match = re.search(r"[\w.+-]+@[^\s\"'>]+", combined_html)
    page_result["dpo_compliance"] = {
        "has_dpo": bool(has_dpo),
        "dpo_email": dpo_email_match.group(0) if dpo_email_match and has_dpo else None,
    }

    # DNC mention detection
    mentions_dnc = "dnc" in combined_html or "do not call" in combined_html or "do-not-call" in combined_html
    page_result["dnc_mention"] = {"mentions_dnc": bool(mentions_dnc)}

    # NRIC hints detection
    nric_word = re.search(r"\bnric\b", combined_html)
    fin_word = re.search(r"\bfin\b", combined_html)
    fin_context = "fin number" in combined_html or "fin no" in combined_html
    input_nric = re.search(r"name=\"[^\"]*(nric|fin)[^\"]*\"", combined_html)
    collects_nric = bool(nric_word or (fin_word and fin_context) or input_nric)
    page_result["collects_nric"] = bool(collects_nric)
    if collects_nric:
        page_result["nric_evidence"] = "NRIC/FIN keyword detected in page content"

    # Cookie banner detection from combined HTML
    cookie_indicators = [
        "cookiebot",
        "usercentrics",
        "cookieyes",
        "onetrust",
        "osano",
        "iubenda",
        "cookie-consent",
        "cookie consent",
        "consentmanager",
        "data-cookieconsent",
    ]
    detected_cookies = [k for k in cookie_indicators if k in combined_html]
    policy_mentions_banner = "cookie banner" in combined_html or "accept all" in combined_html or "reject" in combined_html
    if detected_cookies or policy_mentions_banner:
        page_result["consent_mechanism"] = {
            "has_cookie_banner": True,
            "has_active_consent": True,
            "detected_providers": detected_cookies,
            "policy_mentions_banner": policy_mentions_banner,
        }
    elif "consent_mechanism" not in page_result:
        page_result["consent_mechanism"] = {"has_cookie_banner": False}

    return {"security_headers": headers_result, **page_result}


async def _resolve_website_url(raw_url: str | None) -> dict:
    if not raw_url or not isinstance(raw_url, str):
        return {}

    url = raw_url.strip()
    if not url:
        return {}

    # Normalize input and try HTTPS first, then HTTP.
    normalized = url
    if normalized.lower().startswith("http://"):
        normalized = normalized[7:]
    elif normalized.lower().startswith("https://"):
        normalized = normalized[8:]

    candidates = [f"https://{normalized}", f"http://{normalized}"]

    headers = {"User-Agent": "BooppaComplianceBot/1.0"}

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        for candidate in candidates:
            try:
                resp = await client.get(candidate, headers=headers)
                final_url = str(resp.url)
                return {
                    "resolved_url": final_url,
                    "uses_https": final_url.lower().startswith("https://"),
                    "http_status": resp.status_code,
                }
            except Exception as e:
                logger.warning(f"URL check failed for {candidate}: {e}")

    return {"resolution_error": "all_attempts_failed"}


@celery_app.task(bind=True, max_retries=3, name="process_report_task")
def process_report_task(self, report_id: str):
    """Main report processing task - orchestrates the entire workflow"""
    try:
        # Run async workflow in sync context (use asyncio.run to create a fresh event loop)
        result = asyncio.run(process_report_workflow(report_id))

        logger.info(f"Report {report_id} processed successfully")
        return result

    except Exception as exc:
        logger.error(f"Report processing failed for {report_id}: {exc}")

        # Update report status to failed
        db = SessionLocal()
        try:
            report = db.query(Report).filter(Report.id == report_id).first()
            if report:
                report.status = "failed"
                db.commit()
        finally:
            db.close()

        # Retry with exponential backoff
        countdown = 60 * (2**self.request.retries)  # 1min, 2min, 4min
        raise self.retry(exc=exc, countdown=countdown)


async def process_report_workflow(report_id: str) -> dict:
    """Async workflow for report processing"""
    db = SessionLocal()
    try:
        # Get report from database
        report = db.query(Report).filter(Report.id == report_id).first()
        if not report:
            raise ValueError(f"Report {report_id} not found")

        # Ensure assessment_data is a dict (it might come as a string)
        ad = report.assessment_data or {}
        if not isinstance(ad, dict):
            try:
                ad = json.loads(ad)
            except Exception:
                ad = {}
        
        policy = enforce_tier(ad, report.framework)
        features = policy.get("features", {}) if isinstance(policy, dict) else {}
        
        # Debug logging for tier resolution
        logger.info(f"Tier Resolution for {report_id}: framework={report.framework}, tier={policy.get('tier')}, paid={policy.get('paid')}, pdf_enabled={features.get('pdf')}")
        if isinstance(ad, dict):
            logger.info(f"Payment status for {report_id}: payment_confirmed={ad.get('payment_confirmed')}, product_type={ad.get('product_type')}")
        
        try:
            _set_assessment_values(
                report,
                {
                    "access_checked_at": datetime.utcnow().isoformat(),
                    "access_allowed": policy.get("allowed"),
                    "access_paid": policy.get("paid"),
                    "access_reason": policy.get("reason"),
                    "tier": policy.get("tier"),
                    "tier_features": features,
                },
            )
            db.commit()
        except Exception:
            db.rollback()

        if not policy.get("allowed"):
            report.status = "blocked"
            report.completed_at = datetime.utcnow()
            try:
                _set_assessment_values(
                    report,
                    {
                        "access_blocked": True,
                        "access_blocked_at": datetime.utcnow().isoformat(),
                    },
                )
                db.commit()
            except Exception:
                db.rollback()

            try:
                dep_updates = log_dependency_event(
                    report.assessment_data,
                    owner_id=str(report.owner_id),
                    report_id=str(report.id),
                    company_name=report.company_name,
                    event_type="access_blocked",
                )
                _set_assessment_values(report, dep_updates)
                db.commit()
            except Exception:
                db.rollback()

            return {
                "status": "blocked",
                "report_id": report_id,
                "reason": policy.get("reason"),
            }

        # Resolve website URL over the network and store HTTPS status
        try:
            url = None
            if isinstance(report.assessment_data, dict):
                url = report.assessment_data.get("url") or report.company_website
            result = await _resolve_website_url(url)
            if result:
                resolved_url = result.get("resolved_url")
                updates = {}
                if resolved_url:
                    updates["url"] = resolved_url
                    updates["resolved_url"] = resolved_url
                if "uses_https" in result:
                    updates["uses_https"] = bool(result.get("uses_https"))
                if "http_status" in result:
                    updates["http_status"] = result.get("http_status")
                if result.get("resolution_error"):
                    updates["url_resolution_error"] = result.get("resolution_error")
                if updates:
                    _set_assessment_values(report, updates)
                    db.commit()
        except Exception as e:
            logger.warning(
                f"Could not resolve website URL for {report_id}: {e}"
            )

        # Detect cookie banner/consent mechanism from HTML
        try:
            resolved_url = None
            if isinstance(report.assessment_data, dict):
                resolved_url = report.assessment_data.get("resolved_url") or report.assessment_data.get("url")
            cookie_result = await _detect_cookie_banner(resolved_url)
            if cookie_result:
                _set_assessment_values(report, cookie_result)
                db.commit()
        except Exception as e:
            logger.warning(f"Cookie detection failed for {report_id}: {e}")

        # Run broad website metadata scan (privacy policy, DPO, DNC, security headers, NRIC hints)
        try:
            resolved_url = None
            if isinstance(report.assessment_data, dict):
                resolved_url = report.assessment_data.get("resolved_url") or report.assessment_data.get("url")
            metadata_result = await _scan_site_metadata(resolved_url)
            if metadata_result:
                _set_assessment_values(report, metadata_result)
                db.commit()
        except Exception as e:
            logger.warning(f"Metadata scan failed for {report_id}: {e}")

        # Step 1: Generate structured AI report (full for paid tiers, light for free)
        logger.info(f"Step 1: Generating AI report for {report_id}")
        structured_report = None
        narrative = ""

        if features.get("ai_full"):
            booppa_ai = BooppaAIService()
            structured_report = await booppa_ai.generate_compliance_report(
                report.assessment_data
            )
            # Keep a human-readable narrative for legacy fields
            try:
                ai_service = AIService()
                narrative = ai_service._format_report_as_narrative(structured_report)
            except Exception:
                narrative = structured_report.get("executive_summary") or ""

            report.ai_model_used = structured_report.get("report_metadata", {}).get(
                "ai_model", "Booppa"
            )
            # persist structured report into assessment_data for traceability
            try:
                _set_assessment_values(
                    report,
                    {
                        "booppa_report": structured_report,
                        "booppa_report_saved_at": datetime.utcnow().isoformat(),
                    },
                )
            except Exception:
                logger.warning(
                    "Could not attach structured report into assessment_data"
                )
        else:
            url_value = None
            if isinstance(report.assessment_data, dict):
                url_value = report.assessment_data.get("url")
            light_payload = {
                "company_name": report.company_name,
                "url": url_value or report.company_website,
                "scan_date": datetime.utcnow().strftime("%Y-%m-%d"),
                "detected_laws": (
                    report.assessment_data.get("detected_laws", [])
                    if isinstance(report.assessment_data, dict)
                    else []
                ),
                "overall_risk_score": (
                    report.assessment_data.get("overall_risk_score")
                    if isinstance(report.assessment_data, dict)
                    else 0
                ),
                "uses_https": (
                    report.assessment_data.get("uses_https", True)
                    if isinstance(report.assessment_data, dict)
                    else True
                ),
            }
            light_report = await ai_preview(light_payload)
            narrative = light_report.get("summary") or light_report.get(
                "recommendation", ""
            )
            report.ai_model_used = "Booppa Light"
            try:
                _set_assessment_values(
                    report,
                    {
                        "light_ai_report": light_report,
                        "light_ai_saved_at": datetime.utcnow().isoformat(),
                    },
                )
            except Exception:
                logger.warning("Could not attach light AI output into assessment_data")

        report.ai_narrative = narrative
        db.commit()

        # Step 2: Compute evidence hash
        logger.info(f"Step 2: Computing evidence hash for {report_id}")
        evidence_data = {
            "report_id": str(report.id),
            "framework": report.framework,
            "company": report.company_name,
            "assessment_data": report.assessment_data,
            "ai_narrative": narrative,
            "timestamp": report.created_at.isoformat(),
        }

        evidence_json = json.dumps(evidence_data, sort_keys=True)
        evidence_hash = hashlib.sha256(evidence_json.encode()).hexdigest()
        report.audit_hash = evidence_hash
        db.commit()

        try:
            append_audit_event(
                db,
                report_id=str(report.id),
                action="report_hash_created",
                actor=str(report.owner_id),
                hash_value=evidence_hash,
                metadata={"framework": report.framework},
            )
            db.commit()
        except Exception as e:
            db.rollback()
            logger.warning(f"Failed to append audit chain for {report_id}: {e}")

        # Step 3: Anchor on blockchain (only if payment confirmed)
        logger.info(f"Step 3: Anchoring evidence on blockchain for {report_id}")
        payment_confirmed = bool(policy.get("paid"))

        tx_hash = None
        if features.get("blockchain") and payment_confirmed:
            blockchain = BlockchainService()
            metadata = f"report:{report.id}"
            tx_hash = await blockchain.anchor_evidence(evidence_hash, metadata=metadata)
            report.tx_hash = tx_hash
            db.commit()
        else:
            # leave tx_hash None; PDF will point to pending verification
            report.tx_hash = None
            db.commit()

        verify_base = settings.VERIFY_BASE_URL.rstrip("/")
        verify_url = f"{verify_base}/verify/{evidence_hash}"

        if features.get("pdf") and payment_confirmed:
            try:
                _set_assessment_values(
                    report,
                    {
                        "verify_url": verify_url,
                        "proof_header": "BOOPPA-PROOF-SG",
                        "schema_version": "1.0",
                    },
                )
                db.commit()
            except Exception:
                db.rollback()

        try:
            if features.get("pdf") and payment_confirmed:
                verify_updates = register_verification(
                    report.assessment_data,
                    evidence_hash=evidence_hash,
                    tx_hash=tx_hash,
                )
                _set_assessment_values(report, verify_updates)
                db.commit()
        except Exception as e:
            logger.warning(f"Failed to register verification for {report_id}: {e}")

        # Ensure a site screenshot is present for on-page report (even if PDF is skipped).
        try:
            existing_screenshot = None
            if isinstance(report.assessment_data, dict):
                existing_screenshot = report.assessment_data.get("site_screenshot")
            if not existing_screenshot:
                url = None
                if isinstance(report.assessment_data, dict):
                    url = report.assessment_data.get("url") or report.company_website
                if isinstance(url, str) and url and not url.lower().startswith(("http://", "https://")):
                    url = f"https://{url}"
                if url:
                    ss_b64 = await _capture_screenshot_with_timeout(url, timeout=25)
                    if ss_b64:
                        try:
                            _set_assessment_values(report, {"site_screenshot": ss_b64})
                            db.commit()
                        except Exception as e:
                            logger.warning(
                                f"Could not store site screenshot for {report_id}: {e}"
                            )
                    else:
                        thum_b64, thum_err = await _fetch_thum_io_base64(url)
                        if thum_b64:
                            try:
                                _set_assessment_values(report, {"site_screenshot": thum_b64})
                                db.commit()
                            except Exception as e:
                                logger.warning(
                                    f"Could not store thum.io screenshot for {report_id}: {e}"
                                )
                        else:
                            try:
                                _set_assessment_values(
                                    report,
                                    {
                                        "screenshot_error": thum_err
                                        or "capture_failed_or_timeout",
                                        "screenshot_url": url,
                                    },
                                )
                                db.commit()
                            except Exception as e:
                                logger.warning(
                                    f"Could not store screenshot error for {report_id}: {e}"
                                )
        except Exception as e:
            try:
                _set_assessment_values(
                    report,
                    {
                        "screenshot_error": f"exception:{str(e)[:200]}",
                        "screenshot_url": report.assessment_data.get("url")
                        if isinstance(report.assessment_data, dict)
                        else report.company_website,
                    },
                )
                db.commit()
            except Exception:
                db.rollback()
            logger.warning(
                f"Could not capture site screenshot for {report_id}: {e}"
            )

        # If this report is meant for on-page only, mark as completed and skip PDF generation.
        try:
            on_page_only = False
            if isinstance(report.assessment_data, dict):
                on_page_only = bool(report.assessment_data.get("on_page_only"))
            if not features.get("pdf"):
                _set_assessment_values(
                    report,
                    {
                        "pdf_generated": False,
                        "pdf_reason": "tier_restriction",
                    },
                )
                report.status = "completed"
                report.completed_at = datetime.utcnow()
                db.commit()

                # Send notification email without PDF link
                email_service = EmailService()
                try:
                    to_email = None
                    if isinstance(report.assessment_data, dict):
                        to_email = report.assessment_data.get(
                            "contact_email"
                        ) or report.assessment_data.get("customer_email")
                    if to_email:
                        await email_service.send_report_ready_email(
                            to_email=to_email,
                            report_url=None,
                            user_name=(report.company_name or "User"),
                            report_id=str(report.id),
                        )
                except Exception as e:
                    logger.error(
                        f"Failed to send notification email for {report_id}: {e}"
                    )

                try:
                    dep_updates = log_dependency_event(
                        report.assessment_data,
                        owner_id=str(report.owner_id),
                        report_id=str(report.id),
                        company_name=report.company_name,
                        event_type="report_completed",
                        extra={"delivery": "no_pdf"},
                    )
                    _set_assessment_values(report, dep_updates)
                    db.commit()
                except Exception:
                    db.rollback()

                return {
                    "status": "completed",
                    "report_id": report_id,
                    "pdf_url": None,
                    "tx_hash": tx_hash,
                }
            if on_page_only:
                _set_assessment_values(report, {"on_page_ready": True})
                report.status = "completed"
                report.completed_at = datetime.utcnow()
                db.commit()

                try:
                    dep_updates = log_dependency_event(
                        report.assessment_data,
                        owner_id=str(report.owner_id),
                        report_id=str(report.id),
                        company_name=report.company_name,
                        event_type="report_completed",
                        extra={"delivery": "on_page"},
                    )
                    _set_assessment_values(report, dep_updates)
                    db.commit()
                except Exception:
                    db.rollback()

                return {
                    "status": "completed",
                    "report_id": report_id,
                    "pdf_url": None,
                    "tx_hash": tx_hash,
                }
        except Exception as e:
            logger.warning(f"Failed to finalize on-page report {report_id}: {e}")

        # Optional: skip PDF generation and S3 upload
        if settings.SKIP_PDF_GENERATION:
            logger.info(f"Skipping PDF generation for {report_id}")
            report.s3_url = None
            report.file_key = None
            report.status = "completed"
            report.completed_at = datetime.utcnow()
            try:
                _set_assessment_values(
                    report,
                    {
                        "pdf_generated": False,
                        "s3_uploaded": False,
                    },
                )
                db.commit()
            except Exception:
                db.rollback()

            # Send notification email without PDF link
            email_service = EmailService()
            try:
                to_email = None
                if isinstance(report.assessment_data, dict):
                    to_email = report.assessment_data.get("contact_email") or report.assessment_data.get(
                        "customer_email"
                    )
                if to_email:
                    await email_service.send_report_ready_email(
                        to_email=to_email,
                        report_url=None,
                        user_name=(report.company_name or "User"),
                        report_id=str(report.id),
                    )
            except Exception as e:
                logger.error(
                    f"Failed to send notification email for {report_id}: {e}"
                )

            try:
                dep_updates = log_dependency_event(
                    report.assessment_data,
                    owner_id=str(report.owner_id),
                    report_id=str(report.id),
                    company_name=report.company_name,
                    event_type="report_completed",
                    extra={"delivery": "no_pdf"},
                )
                _set_assessment_values(report, dep_updates)
                db.commit()
            except Exception:
                db.rollback()

            return {
                "status": "completed",
                "report_id": report_id,
                "pdf_url": None,
                "tx_hash": tx_hash,
            }

        # Step 4: Generate PDF with QR code
        logger.info(f"Step 4: Generating PDF for {report_id}")
        pdf_service = PDFService()

        pdf_data = {
            "report_id": str(report.id),
            "framework": report.framework,
            "company_name": report.company_name,
            "created_at": report.created_at.isoformat(),
            "status": "completed",
            "tx_hash": tx_hash,
            "audit_hash": evidence_hash,
            "ai_narrative": narrative,
            "structured_report": structured_report,
            "payment_confirmed": payment_confirmed,
            "tier": policy.get("tier"),
            "proof_header": (
                report.assessment_data.get("proof_header")
                if isinstance(report.assessment_data, dict)
                else None
            )
            or ("BOOPPA-PROOF-SG" if payment_confirmed else None),
            "schema_version": (
                report.assessment_data.get("schema_version")
                if isinstance(report.assessment_data, dict)
                else None
            )
            or ("1.0" if payment_confirmed else None),
            "verify_url": (
                report.assessment_data.get("verify_url")
                if isinstance(report.assessment_data, dict)
                else None
            )
            or (verify_url if payment_confirmed else None),
            "contact_email": (
                report.assessment_data.get("contact_email")
                if isinstance(report.assessment_data, dict)
                else None
            ),
            "base_url": (
                report.assessment_data.get("base_url")
                if isinstance(report.assessment_data, dict)
                and report.assessment_data.get("base_url")
                else "https://www.booppa.io"
            ),
        }

        # Ensure a site screenshot is present for every PDF. Prefer existing data, otherwise capture.
        if not pdf_data.get("site_screenshot"):
            try:
                url = None
                if isinstance(report.assessment_data, dict):
                    url = report.assessment_data.get("url") or report.company_website
                if url:
                    ss_b64 = await _capture_screenshot_with_timeout(url, timeout=25)
                    if ss_b64:
                        pdf_data["site_screenshot"] = ss_b64
                        try:
                            _set_assessment_values(report, {"site_screenshot": ss_b64})
                            db.commit()
                        except Exception as e:
                            logger.warning(
                                f"Could not store site screenshot for {report_id}: {e}"
                            )
                    else:
                        thum_b64, thum_err = await _fetch_thum_io_base64(url)
                        if thum_b64:
                            pdf_data["site_screenshot"] = thum_b64
                            try:
                                _set_assessment_values(report, {"site_screenshot": thum_b64})
                                db.commit()
                            except Exception as e:
                                logger.warning(
                                    f"Could not store thum.io screenshot for {report_id}: {e}"
                                )
                        else:
                            try:
                                _set_assessment_values(
                                    report,
                                    {
                                        "screenshot_error": thum_err
                                        or "capture_failed_or_timeout",
                                        "screenshot_url": url,
                                    },
                                )
                                db.commit()
                            except Exception as e:
                                logger.warning(
                                    f"Could not store screenshot error for {report_id}: {e}"
                                )
            except Exception as e:
                try:
                    _set_assessment_values(
                        report,
                        {
                            "screenshot_error": f"exception:{str(e)[:200]}",
                            "screenshot_url": report.assessment_data.get("url")
                            if isinstance(report.assessment_data, dict)
                            else report.company_website,
                        },
                    )
                    db.commit()
                except Exception:
                    db.rollback()
                logger.warning(
                    f"Could not capture site screenshot for {report_id}: {e}"
                )

        try:
            pdf_bytes = pdf_service.generate_pdf(pdf_data)
            logger.info(
                f"PDF generated for {report_id} ({len(pdf_bytes)} bytes)"
            )
            try:
                _set_assessment_values(
                    report,
                    {
                        "pdf_generated": True,
                        "pdf_generated_at": datetime.utcnow().isoformat(),
                    },
                )
                db.commit()
            except Exception:
                db.rollback()
        except Exception as e:
            logger.error(f"PDF generation failed for {report_id}: {e}")
            raise

        # Step 5: Upload to S3 with retry/backoff
        logger.info(f"Step 5: Uploading PDF to S3 for {report_id}")
        storage = S3Service()
        max_attempts = 3
        pdf_url = None
        for attempt in range(1, max_attempts + 1):
            try:
                pdf_url = await storage.upload_pdf(pdf_bytes, str(report.id))
                report.s3_url = pdf_url
                report.file_key = f"reports/{report.id}.pdf"
                try:
                    _set_assessment_values(
                        report,
                        {
                            "s3_uploaded": True,
                            "s3_uploaded_at": datetime.utcnow().isoformat(),
                        },
                    )
                except Exception:
                    logger.warning(f"Failed to mark S3 upload status for {report_id}")
                # Mark as completed once upload succeeds so frontend can access URL
                report.status = "completed"
                report.completed_at = datetime.utcnow()
                db.commit()
                break
            except Exception as e:
                logger.error(f"S3 upload attempt {attempt} failed for {report_id}: {e}")
                if attempt == max_attempts:
                    # propagate so workflow marks failed and triggers retry
                    raise
                await asyncio.sleep(min(10, 2**attempt))

        # Step 6: Send notification email (non-fatal)
        logger.info(f"Step 6: Sending notification for {report_id}")
        email_service = EmailService()
        try:
            to_email = None
            if isinstance(report.assessment_data, dict):
                to_email = report.assessment_data.get("contact_email") or report.assessment_data.get(
                    "customer_email"
                )
            if not to_email:
                raise ValueError("Missing contact email for report notification")

            await email_service.send_report_ready_email(
                to_email=to_email,
                report_url=pdf_url,
                user_name=(report.company_name or "User"),
                report_id=str(report.id),
            )
        except Exception as e:
            logger.error(f"Failed to send notification email for {report_id}: {e}")

        # If not already marked completed (defensive), set completion timestamp
        try:
            if report.status != "completed":
                report.status = "completed"
                report.completed_at = datetime.utcnow()
                db.commit()
        except Exception:
            db.rollback()

        try:
            dep_updates = log_dependency_event(
                report.assessment_data,
                owner_id=str(report.owner_id),
                report_id=str(report.id),
                company_name=report.company_name,
                event_type="report_completed",
                extra={"delivery": "pdf" if pdf_url else "no_pdf"},
            )
            _set_assessment_values(report, dep_updates)
            db.commit()
        except Exception:
            db.rollback()

        return {
            "status": "completed",
            "report_id": report_id,
            "pdf_url": pdf_url,
            "tx_hash": tx_hash,
        }

    except Exception as e:
        db.rollback()
        logger.error(f"Report workflow failed: {e}")
        try:
            report = db.query(Report).filter(Report.id == report_id).first()
            if report:
                assessment = report.assessment_data or {}
                if not isinstance(assessment, dict):
                    assessment = {}
                assessment["last_processing_error"] = str(e)[:500]
                assessment["last_processing_error_at"] = datetime.utcnow().isoformat()
                report.assessment_data = assessment
                report.status = "failed"
                db.commit()
        except Exception as inner_exc:
            logger.error(
                f"Failed to persist processing error for {report_id}: {inner_exc}"
            )
        raise
    finally:
        db.close()


@celery_app.task(bind=True, max_retries=3, name="fulfill_vendor_proof_task")
def fulfill_vendor_proof_task(self, report_id: str, customer_email: str | None = None):
    """Celery task: create VerifyRecord, set compliance baseline, send badge email."""
    try:
        from app.api.stripe_webhook import _fulfill_vendor_proof
        asyncio.run(_fulfill_vendor_proof(report_id=report_id, customer_email=customer_email))
        logger.info(f"Vendor proof fulfilled for report {report_id}")
    except Exception as exc:
        logger.error(f"Vendor proof fulfillment failed for {report_id}: {exc}")
        countdown = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=countdown)


@celery_app.task(bind=True, max_retries=3, name="fulfill_pdpa_task")
def fulfill_pdpa_task(self, report_id: str, customer_email: str | None = None):
    """Celery task: generate PDPA PDF, update compliance score, write CertificateLog, send email."""
    try:
        from app.api.stripe_webhook import _fulfill_pdpa
        asyncio.run(_fulfill_pdpa(report_id=report_id, customer_email=customer_email))
        logger.info(f"PDPA snapshot fulfilled for report {report_id}")
    except Exception as exc:
        logger.error(f"PDPA fulfillment failed for {report_id}: {exc}")
        countdown = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=countdown)


@celery_app.task(bind=True, max_retries=3, name="fulfill_notarization_task")
def fulfill_notarization_task(self, report_id: str, customer_email: str | None = None):
    """Celery task: anchor, generate PDF, and deliver notarization certificate."""
    try:
        from app.api.stripe_webhook import _fulfill_notarization
        asyncio.run(_fulfill_notarization(report_id=report_id, customer_email=customer_email))
        logger.info(f"Notarization fulfilled for report {report_id}")
    except Exception as exc:
        logger.error(f"Notarization fulfillment failed for {report_id}: {exc}")
        countdown = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=countdown)


@celery_app.task(bind=True, max_retries=3, name="fulfill_rfp_task")
def fulfill_rfp_task(
    self,
    product_type: str,
    vendor_id: str,
    vendor_email: str,
    vendor_url: str,
    company_name: str,
    rfp_description: str | None = None,
    session_id: str | None = None,
    intake_data: dict | None = None,
):
    """Celery task: generate and deliver the RFP Kit evidence package."""
    try:
        from app.api.stripe_webhook import _fulfill_rfp_package
        asyncio.run(_fulfill_rfp_package(
            product_type=product_type,
            vendor_id=vendor_id,
            vendor_email=vendor_email,
            vendor_url=vendor_url,
            company_name=company_name,
            rfp_description=rfp_description,
            session_id=session_id,
            intake_data=intake_data,
        ))
        logger.info(f"RFP package fulfilled for vendor {vendor_id} session {session_id}")
    except Exception as exc:
        logger.error(f"RFP fulfillment failed for vendor {vendor_id}: {exc}")
        try:
            from celery.exceptions import MaxRetriesExceededError
            countdown = 60 * (2 ** self.request.retries)
            raise self.retry(exc=exc, countdown=countdown)
        except MaxRetriesExceededError:
            logger.error(f"RFP fulfillment permanently failed for vendor {vendor_id} after {self.max_retries} retries")
            if session_id:
                from app.core.cache import cache as cache_mod
                cache_mod.set(
                    cache_mod.cache_key(f"rfp_result:{session_id}"),
                    {"error": True, "detail": "Generation failed. Please contact support."},
                    ttl=86400
                )
            raise


@celery_app.task(bind=True, max_retries=2, name="vendor_active_health_check_task")
def vendor_active_health_check_task(self, vendor_id: str, vendor_email: str):
    """
    Monthly health check for Vendor Active subscribers.
    1. Recalculate vendor score
    2. Send monthly metrics email (profile views, search appearances, movement vs prior month)
    3. Competitor alert: notify if any sector peer improved verificationDepth this month
    """
    db = SessionLocal()
    try:
        from app.services.scoring import VendorScoreEngine
        from app.services.email_service import EmailService
        from app.core.models import VendorScore, User
        from datetime import timedelta

        # 1. Recalculate score
        score_record = VendorScoreEngine.update_vendor_score(db, vendor_id)

        # 2. Build metrics summary
        user = db.query(User).filter(User.id == vendor_id).first()
        company = getattr(user, "company", "Your company") if user else "Your company"

        thirty_days_ago = datetime.utcnow() - timedelta(days=30)
        from app.core.models import VerifyRecord, ProofView
        verify = db.query(VerifyRecord).filter(VerifyRecord.vendor_id == vendor_id).first()
        profile_views = 0
        if verify:
            profile_views = db.query(ProofView).filter(
                ProofView.verify_id == verify.id,
                ProofView.created_at >= thirty_days_ago,
            ).count()

        # 3. Email monthly digest
        email_svc = EmailService()
        asyncio.run(email_svc.send_html_email(
            to_email=vendor_email,
            subject=f"Your Monthly BOOPPA Health Check — {company}",
            body_html=f"""
            <html><body style="font-family:Arial,sans-serif;color:#0f172a;max-width:600px;margin:0 auto;">
              <div style="background:#0f172a;padding:24px 32px;border-radius:12px 12px 0 0;">
                <h1 style="color:#10b981;margin:0;font-size:20px;">Monthly Profile Health Check</h1>
              </div>
              <div style="padding:32px;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px;">
                <p>Hello <strong>{company}</strong>,</p>
                <p>Here is your BOOPPA profile activity for the past 30 days:</p>
                <div style="background:#f8fafc;border-radius:8px;padding:20px;margin:20px 0;">
                  <p style="margin:4px 0;"><strong>Trust Score:</strong> {score_record.total_score}/100</p>
                  <p style="margin:4px 0;"><strong>Compliance Score:</strong> {score_record.compliance_score}/100</p>
                  <p style="margin:4px 0;"><strong>Profile Views (30d):</strong> {profile_views}</p>
                </div>
                <p>
                  <a href="https://www.booppa.io/vendor/dashboard"
                     style="background:#10b981;color:#fff;padding:12px 24px;text-decoration:none;
                            border-radius:8px;font-weight:bold;display:inline-block;">
                    View Full Dashboard →
                  </a>
                </p>
                <p style="color:#64748b;font-size:12px;margin-top:24px;">
                  Vendor Active — monthly health check · booppa.io
                </p>
              </div>
            </body></html>
            """,
        ))
        logger.info(f"Vendor Active health check completed for vendor {vendor_id}")
    except Exception as exc:
        logger.error(f"Vendor Active health check failed for {vendor_id}: {exc}")
        raise self.retry(exc=exc, countdown=300)
    finally:
        db.close()


@celery_app.task(bind=True, max_retries=2, name="pdpa_monitor_quarterly_rescan_task")
def pdpa_monitor_quarterly_rescan_task(self, vendor_id: str, vendor_email: str, website_url: str):
    """
    Quarterly PDPA re-scan for PDPA Monitor subscribers.
    Creates a new PDPA report and queues fulfill_pdpa_task.
    """
    db = SessionLocal()
    try:
        from app.core.models import Report, User
        import uuid as _uuid

        user = db.query(User).filter(User.id == vendor_id).first()
        company = getattr(user, "company", "Customer") if user else "Customer"

        stub = Report(
            owner_id=_uuid.UUID(vendor_id),
            framework="pdpa_quick_scan",
            company_name=company,
            company_website=website_url,
            status="pending",
            assessment_data={
                "payment_confirmed": True,
                "on_page_only": False,
                "tier": "PRO",
                "contact_email": vendor_email,
                "triggered_by": "pdpa_monitor_quarterly",
            },
        )
        db.add(stub)
        db.commit()
        db.refresh(stub)

        from app.api.stripe_webhook import _fulfill_pdpa
        asyncio.run(_fulfill_pdpa(report_id=str(stub.id), customer_email=vendor_email))
        logger.info(f"PDPA Monitor quarterly re-scan complete for vendor {vendor_id}")
    except Exception as exc:
        logger.error(f"PDPA Monitor quarterly re-scan failed for {vendor_id}: {exc}")
        raise self.retry(exc=exc, countdown=600)
    finally:
        db.close()


@celery_app.task(name="run_vendor_active_monthly_checks")
def run_vendor_active_monthly_checks():
    """
    Beat task: runs on the 1st of each month.
    Finds all active Vendor Active subscribers and queues health checks.
    """
    db = SessionLocal()
    try:
        from app.core.models import User
        subscribers = db.query(User).filter(User.plan == "vendor_active").all()
        for user in subscribers:
            if user.email:
                vendor_active_health_check_task.delay(str(user.id), user.email)
        logger.info(f"Queued monthly health checks for {len(subscribers)} Vendor Active subscribers")
    finally:
        db.close()


@celery_app.task(name="run_pdpa_monitor_quarterly_rescans")
def run_pdpa_monitor_quarterly_rescans():
    """
    Beat task: runs on the 1st of Jan, Apr, Jul, Oct.
    Finds all PDPA Monitor subscribers and queues re-scans.
    """
    db = SessionLocal()
    try:
        from app.core.models import User
        subscribers = db.query(User).filter(User.plan == "pdpa_monitor").all()
        for user in subscribers:
            website = getattr(user, "website", "") or ""
            if user.email and website:
                pdpa_monitor_quarterly_rescan_task.delay(str(user.id), user.email, website)
        logger.info(f"Queued quarterly PDPA re-scans for {len(subscribers)} PDPA Monitor subscribers")
    finally:
        db.close()


@celery_app.task(name="refresh_gebiz_base_rates")
def refresh_gebiz_base_rates():
    """
    4.5: Fetch GeBIZ Government Procurement Awards from data.gov.sg and
    update TenderShortlist.base_rate with real sector/agency win rates.

    Algorithm:
      base_rate = (unique vendors awarded in sector) / (total unique tenders in sector)
      clamped to [0.05, 0.60]

    Runs weekly via Celery Beat. Non-fatal — failures logged, no rollback needed.
    """
    import asyncio as _asyncio

    async def _fetch_and_update():
        from app.core.models_v10 import TenderShortlist
        db = SessionLocal()
        try:
            # ── 1. Fetch GeBIZ award data from data.gov.sg ──────────────────────
            # Dataset: Government Procurement Awards
            # Resource IDs to try in order (primary + fallback)
            GEBIZ_DATASET_IDS = [
                "d_a2c0b1c04e3e55e4e8d39f86b42b0e57",  # Government Procurement Awards
                "5ab68aac-91f6-4f39-9b21-698610bdf3f7",  # Fallback
            ]
            SECTOR_KEYWORDS = {
                "IT": ["information technology", "ict", "software", "hardware", "digital", "cyber", "data"],
                "CONSTRUCTION": ["construction", "building", "infrastructure", "civil"],
                "PROFESSIONAL_SERVICES": ["consultancy", "consulting", "professional services", "advisory"],
                "HEALTHCARE": ["healthcare", "health", "medical", "hospital"],
                "SECURITY": ["security", "surveillance", "guarding"],
                "FACILITIES": ["facilities", "maintenance", "cleaning", "property"],
                "LOGISTICS": ["logistics", "transport", "delivery", "freight"],
                "EDUCATION": ["education", "training", "learning"],
            }
            DEFAULT_BASE_RATE = 0.20
            CLAMP_MIN = 0.05
            CLAMP_MAX = 0.60
            PAGE_SIZE = 1000

            sector_awards: dict[str, int] = {}   # sector → awarded tender count
            sector_tenders: dict[str, int] = {}  # sector → total tenders seen
            agency_awards: dict[str, int] = {}   # agency → awarded count
            agency_tenders: dict[str, int] = {}  # agency → total seen

            fetched_any = False
            async with httpx.AsyncClient(timeout=30) as client:
                for dataset_id in GEBIZ_DATASET_IDS:
                    offset = 0
                    while True:
                        try:
                            resp = await client.get(
                                "https://data.gov.sg/api/action/datastore_search",
                                params={
                                    "resource_id": dataset_id,
                                    "limit": PAGE_SIZE,
                                    "offset": offset,
                                },
                                headers={"User-Agent": "BooppaBot/1.0"},
                            )
                        except Exception as e:
                            logger.warning(f"[GeBIZ] Fetch error dataset={dataset_id} offset={offset}: {e}")
                            break

                        if resp.status_code != 200:
                            logger.warning(f"[GeBIZ] HTTP {resp.status_code} for dataset {dataset_id}")
                            break

                        data = resp.json()
                        records = data.get("result", {}).get("records", [])
                        if not records:
                            break

                        fetched_any = True
                        for rec in records:
                            # Normalise field names across dataset schema variants
                            description = (
                                rec.get("tender_description")
                                or rec.get("award_details")
                                or rec.get("description", "")
                            ).lower()
                            agency = (
                                rec.get("agency")
                                or rec.get("procuring_entity", "UNKNOWN")
                            ).upper().strip()
                            awarded = bool(
                                rec.get("awarded_date")
                                or rec.get("supplier_name")
                                or rec.get("award_amt")
                            )

                            # Classify sector from description keywords
                            matched_sector = "OTHER"
                            for sector, keywords in SECTOR_KEYWORDS.items():
                                if any(kw in description for kw in keywords):
                                    matched_sector = sector
                                    break

                            sector_tenders[matched_sector] = sector_tenders.get(matched_sector, 0) + 1
                            agency_tenders[agency] = agency_tenders.get(agency, 0) + 1
                            if awarded:
                                sector_awards[matched_sector] = sector_awards.get(matched_sector, 0) + 1
                                agency_awards[agency] = agency_awards.get(agency, 0) + 1

                        total = data.get("result", {}).get("total", 0)
                        offset += PAGE_SIZE
                        if offset >= total:
                            break

                    if fetched_any:
                        break  # got data from first working dataset

            if not fetched_any:
                logger.warning("[GeBIZ] No data fetched from any dataset — base_rates unchanged")
                return

            # ── 2. Compute sector rates ─────────────────────────────────────────
            def _rate(awarded: int, total: int) -> float:
                if total == 0:
                    return DEFAULT_BASE_RATE
                return max(CLAMP_MIN, min(CLAMP_MAX, awarded / total))

            sector_rates = {
                s: _rate(sector_awards.get(s, 0), sector_tenders[s])
                for s in sector_tenders
            }
            agency_rates = {
                a: _rate(agency_awards.get(a, 0), agency_tenders[a])
                for a in agency_tenders
            }

            logger.info(f"[GeBIZ] Sector rates computed: {sector_rates}")
            logger.info(f"[GeBIZ] Top agency rates (sample): {dict(list(agency_rates.items())[:5])}")

            # ── 3. Update TenderShortlist.base_rate ─────────────────────────────
            tenders = db.query(TenderShortlist).all()
            updated = 0
            for tender in tenders:
                # Prefer agency-specific rate; fall back to sector rate; then default
                agency_key = (tender.agency or "").upper().strip()
                sector_key = (tender.sector or "OTHER").upper().strip()
                new_rate = (
                    agency_rates.get(agency_key)
                    or sector_rates.get(sector_key)
                    or DEFAULT_BASE_RATE
                )
                if abs(new_rate - tender.base_rate) > 0.005:
                    tender.base_rate = round(new_rate, 4)
                    updated += 1

            db.commit()
            logger.info(
                f"[GeBIZ] base_rate refresh complete: {updated}/{len(tenders)} tenders updated "
                f"from {sum(sector_tenders.values())} award records"
            )

        except Exception as e:
            logger.error(f"[GeBIZ] base_rate refresh failed: {e}")
            db.rollback()
        finally:
            db.close()

    _asyncio.run(_fetch_and_update())


@celery_app.task(name="sync_gebiz_tenders")
def sync_gebiz_tenders():
    """
    Fetch live GeBIZ open tenders via RSS (primary) then scrape the public
    listing (supplementary). Runs every 30 minutes via Celery Beat.
    Respects robots.txt: only public pages are accessed.
    """
    from app.services.gebiz_service import fetch_from_rss, scrape_gebiz_page

    db = SessionLocal()
    try:
        rss_count = fetch_from_rss(db)
        scrape_count = scrape_gebiz_page(db)
        logger.info(f"[GeBIZ] sync complete: rss={rss_count}, scrape={scrape_count}")

        # Bridge GebizTender → TenderShortlist so the probability engine can
        # score any RSS-synced tender without requiring a manual admin entry.
        _bridge_gebiz_to_shortlist(db)
    except Exception as exc:
        logger.error(f"[GeBIZ] sync_gebiz_tenders failed: {exc}")
        db.rollback()
    finally:
        db.close()


def _bridge_gebiz_to_shortlist(db) -> None:
    """Upsert open GebizTenders into TenderShortlist with a default base_rate."""
    from app.core.models_gebiz import GebizTender
    from app.core.models_v10 import TenderShortlist
    from app.services.tender_service import _CATEGORY_TO_SECTOR

    open_tenders = (
        db.query(GebizTender)
        .filter(GebizTender.status == "Open")
        .all()
    )
    bridged = 0
    for gt in open_tenders:
        existing = db.query(TenderShortlist).filter(
            TenderShortlist.tender_no == gt.tender_no
        ).first()
        raw = gt.raw_data or {}
        cat = raw.get("category", "")
        sector = _CATEGORY_TO_SECTOR.get(cat, "General")
        if existing:
            # Keep base_rate; just refresh description and agency
            existing.description = gt.title or existing.description
            existing.agency = gt.agency or existing.agency
        else:
            db.add(TenderShortlist(
                tender_no=gt.tender_no,
                description=gt.title,
                agency=gt.agency or "Government Agency",
                sector=sector,
                base_rate=0.20,
            ))
            bridged += 1
    db.commit()
    if bridged:
        logger.info(f"[GeBIZ] Bridged {bridged} new tenders into TenderShortlist")


@celery_app.task(name="cleanup_old_tasks")
def cleanup_old_tasks():
    """Clean up old completed reports and temporary data"""
    db = SessionLocal()
    try:
        # Delete reports older than 30 days
        cutoff_date = datetime.utcnow() - timedelta(days=30)

        old_reports = (
            db.query(Report)
            .filter(Report.status == "completed", Report.created_at < cutoff_date)
            .all()
        )

        for report in old_reports:
            # In production, you might archive instead of delete
            db.delete(report)

        db.commit()
        logger.info(f"Cleaned up {len(old_reports)} old reports")

    except Exception as e:
        db.rollback()
        logger.error(f"Cleanup failed: {e}")
    finally:
        db.close()


@celery_app.task(name="send_weekly_vendor_scores")
def send_weekly_vendor_scores():
    """
    Send every active vendor their weekly compliance score summary.
    Runs every Monday at 08:00 UTC via Celery Beat.
    Non-fatal — individual email failures are logged and skipped.
    """
    from app.core.models import User
    from app.core.models_v6 import VendorScore

    db = SessionLocal()
    sent = 0
    failed = 0
    try:
        rows = (
            db.query(User, VendorScore)
            .join(VendorScore, VendorScore.vendor_id == User.id)
            .filter(User.is_active == True)
            .all()
        )
        email_svc = EmailService()
        for user, score in rows:
            try:
                subject = f"Your BOOPPA Vendor Score This Week — {score.total_score} pts"
                body_html = f"""
                <html><body style="font-family:Arial,sans-serif;background:#0a0a0a;color:#e5e5e5;padding:32px;">
                <div style="max-width:560px;margin:0 auto;">
                  <h2 style="color:#ffffff;">Your Weekly Vendor Score</h2>
                  <p>Hi {user.full_name or user.company or user.email},</p>
                  <p>Here's how your BOOPPA compliance profile performed this week:</p>
                  <table style="width:100%;border-collapse:collapse;margin:16px 0;">
                    <tr><td style="padding:8px 0;color:#a3a3a3;">Compliance</td>
                        <td style="padding:8px 0;text-align:right;font-weight:bold;color:#60a5fa;">{score.compliance_score}</td></tr>
                    <tr><td style="padding:8px 0;color:#a3a3a3;">Visibility</td>
                        <td style="padding:8px 0;text-align:right;font-weight:bold;color:#60a5fa;">{score.visibility_score}</td></tr>
                    <tr><td style="padding:8px 0;color:#a3a3a3;">Engagement</td>
                        <td style="padding:8px 0;text-align:right;font-weight:bold;color:#60a5fa;">{score.engagement_score}</td></tr>
                    <tr><td style="padding:8px 0;color:#a3a3a3;">Procurement Interest</td>
                        <td style="padding:8px 0;text-align:right;font-weight:bold;color:#60a5fa;">{score.procurement_interest_score}</td></tr>
                    <tr style="border-top:1px solid #262626;">
                      <td style="padding:12px 0;color:#ffffff;font-weight:bold;">Total Score</td>
                      <td style="padding:12px 0;text-align:right;font-size:1.4em;font-weight:bold;color:#a78bfa;">{score.total_score}</td>
                    </tr>
                  </table>
                  <a href="https://www.booppa.io/vendor/dashboard"
                     style="display:inline-block;background:#7c3aed;color:#ffffff;padding:12px 24px;
                            border-radius:8px;text-decoration:none;font-weight:bold;margin-top:8px;">
                    View Full Dashboard
                  </a>
                  <p style="margin-top:24px;font-size:0.8em;color:#525252;">
                    You're receiving this because you have an active BOOPPA vendor profile.
                    <a href="https://www.booppa.io/vendor/profile" style="color:#7c3aed;">Manage preferences</a>
                  </p>
                </div>
                </body></html>
                """
                import asyncio as _asyncio
                _asyncio.run(email_svc.send_html_email(user.email, subject, body_html))
                sent += 1
            except Exception as exc:
                logger.warning(f"[WeeklyScore] Failed to send to {user.email}: {exc}")
                failed += 1
    except Exception as exc:
        logger.error(f"[WeeklyScore] Task aborted: {exc}")
    finally:
        db.close()

    logger.info(f"[WeeklyScore] Sent={sent} Failed={failed}")


@celery_app.task(name="send_gebiz_alert_newsletter")
def send_gebiz_alert_newsletter():
    """
    Send every active vendor a curated list of GeBIZ tenders closing within 14 days.
    Runs every Monday at 07:00 UTC via Celery Beat (one hour before the score digest).
    Non-fatal — individual email failures are logged and skipped.
    """
    from app.core.models import User
    from app.core.models_gebiz import GebizTender
    from datetime import timedelta

    db = SessionLocal()
    sent = 0
    failed = 0
    try:
        now = datetime.utcnow()
        deadline = now + timedelta(days=14)

        tenders = (
            db.query(GebizTender)
            .filter(
                GebizTender.status == "Open",
                GebizTender.closing_date >= now,
                GebizTender.closing_date <= deadline,
            )
            .order_by(GebizTender.closing_date.asc())
            .limit(10)
            .all()
        )

        if not tenders:
            logger.info("[GeBIZAlert] No tenders closing within 14 days — skipping newsletter")
            return

        # Build the tender rows HTML once, reuse per vendor
        rows_html = ""
        for t in tenders:
            days_left = (t.closing_date - now).days if t.closing_date else "?"
            value_str = f"S${t.estimated_value:,.0f}" if t.estimated_value else "Not disclosed"
            tender_url = t.url or f"https://www.gebiz.gov.sg"
            rows_html += f"""
            <tr>
              <td style="padding:10px 8px;border-bottom:1px solid #262626;color:#e5e5e5;">
                <a href="{tender_url}" style="color:#a78bfa;text-decoration:none;font-weight:500;">{t.tender_no}</a><br>
                <span style="font-size:0.85em;color:#a3a3a3;">{t.title[:120]}</span>
              </td>
              <td style="padding:10px 8px;border-bottom:1px solid #262626;color:#a3a3a3;white-space:nowrap;">{t.agency}</td>
              <td style="padding:10px 8px;border-bottom:1px solid #262626;color:#60a5fa;white-space:nowrap;">{value_str}</td>
              <td style="padding:10px 8px;border-bottom:1px solid #262626;white-space:nowrap;">
                <span style="color:{'#ef4444' if isinstance(days_left, int) and days_left <= 3 else '#f59e0b' if isinstance(days_left, int) and days_left <= 7 else '#10b981'}">
                  {days_left}d left
                </span>
              </td>
            </tr>"""

        vendors = db.query(User).filter(User.is_active == True).all()
        email_svc = EmailService()

        for vendor in vendors:
            try:
                subject = f"GeBIZ Alert: {len(tenders)} tenders closing in the next 14 days"
                body_html = f"""
                <html><body style="font-family:Arial,sans-serif;background:#0a0a0a;color:#e5e5e5;padding:32px;">
                <div style="max-width:640px;margin:0 auto;">
                  <p style="font-size:0.8em;color:#525252;text-transform:uppercase;letter-spacing:0.1em;margin-bottom:4px;">
                    BOOPPA · GeBIZ Intelligence
                  </p>
                  <h2 style="color:#ffffff;margin-top:0;">Tenders Closing Soon</h2>
                  <p style="color:#a3a3a3;">
                    Hi {vendor.full_name or vendor.company or vendor.email},<br>
                    Here are the GeBIZ opportunities closing within the next 14 days.
                    Check your win probability before you bid.
                  </p>

                  <table style="width:100%;border-collapse:collapse;margin:20px 0;font-size:0.9em;">
                    <thead>
                      <tr style="border-bottom:1px solid #404040;">
                        <th style="padding:8px;text-align:left;color:#737373;font-weight:600;">Tender</th>
                        <th style="padding:8px;text-align:left;color:#737373;font-weight:600;">Agency</th>
                        <th style="padding:8px;text-align:left;color:#737373;font-weight:600;">Est. Value</th>
                        <th style="padding:8px;text-align:left;color:#737373;font-weight:600;">Deadline</th>
                      </tr>
                    </thead>
                    <tbody>{rows_html}</tbody>
                  </table>

                  <div style="margin:24px 0;display:flex;gap:12px;">
                    <a href="https://www.booppa.io/tender-check"
                       style="display:inline-block;background:#7c3aed;color:#ffffff;padding:12px 24px;
                              border-radius:8px;text-decoration:none;font-weight:bold;">
                      Check Win Probability →
                    </a>
                    <a href="https://www.booppa.io/opportunities"
                       style="display:inline-block;background:#1a1a1a;color:#a78bfa;padding:12px 24px;
                              border-radius:8px;text-decoration:none;font-weight:bold;border:1px solid #404040;">
                      View All Open Tenders
                    </a>
                  </div>

                  <p style="margin-top:24px;font-size:0.8em;color:#525252;">
                    You're receiving this because you have an active BOOPPA vendor profile.
                    <a href="https://www.booppa.io/vendor/profile" style="color:#7c3aed;">Manage preferences</a>
                  </p>
                </div>
                </body></html>
                """
                import asyncio as _asyncio
                _asyncio.run(email_svc.send_html_email(vendor.email, subject, body_html))
                sent += 1
            except Exception as exc:
                logger.warning(f"[GeBIZAlert] Failed to send to {vendor.email}: {exc}")
                failed += 1
    except Exception as exc:
        logger.error(f"[GeBIZAlert] Task aborted: {exc}")
    finally:
        db.close()

    logger.info(f"[GeBIZAlert] Tenders={len(tenders)} Sent={sent} Failed={failed}")
