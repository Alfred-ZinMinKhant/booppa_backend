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
from app.integrations.ai.adapter import ai_light
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
        # Run async workflow in sync context
        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(process_report_workflow(report_id))

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
            light_report = await ai_light(light_payload)
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
        verify_url = f"{verify_base}/{evidence_hash}"

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
