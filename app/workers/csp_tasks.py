"""
Booppa CSP Compliance Pack — Celery Tasks

Ported onto Booppa's Celery + service layer:
  - @shared_task -> @celery_app.task (registered via celery_app `include`)
  - app.database.SessionLocal -> app.core.db.SessionLocal
  - missing helper `notarize_document_hash` -> `_notarize_document_hash` shim around the
    async BlockchainService.anchor_evidence (run via asyncio.run); network follows
    settings.USE_MAINNET (Amoy testnet by default)
  - missing `remediation_pdf.generate_policy_document_pdf` -> csp_doc_generator.generate_csp_document_pdf

FIX #2 (preserved verbatim): notarize_csp_record builds a DETERMINISTIC SHA-256 hash from
the serialized record content — not from datetime.now() — so two notarizations of the same
record produce the same hash, making the blockchain evidence tamper-evident.

FIX #3 (preserved): CDD/UBO/manual screening runs OFAC + UN sanctions screening; hits escalate
the client to VERY_HIGH risk + edd_required.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, Optional

from app.workers.celery_app import celery_app
from app.core.config import settings

logger = logging.getLogger(__name__)


def _db():
    from app.core.db import SessionLocal
    return SessionLocal()


def _s3_upload(pdf_bytes: bytes, key: str) -> str:
    import boto3
    bucket = getattr(settings, "AWS_S3_BUCKET", None) or os.environ.get("AWS_S3_BUCKET", "booppa-documents")
    boto3.client("s3").put_object(
        Bucket=bucket, Key=key, Body=pdf_bytes,
        ContentType="application/pdf",
        ServerSideEncryption="AES256",
    )
    return key


def _notarize_document_hash(document_hash: str, metadata: str = "", audit_id: str = ""):
    """Shim replacing the pack's assumed `notarize_document_hash`.

    Anchors a SHA-256 hex hash on-chain via Booppa's BlockchainService (async, run in a
    fresh event loop here since Celery tasks are synchronous). Returns a lightweight object
    exposing the attributes the pack code reads: tx_hash, timestamp, block_number, network,
    polygonscan_url, gas_used.
    """
    from app.services.blockchain import BlockchainService

    meta = f"csp:{metadata}:{audit_id}" if audit_id else f"csp:{metadata}"
    tx_hash = asyncio.run(
        BlockchainService().anchor_evidence(document_hash, metadata=meta, force=False)
    )
    explorer = settings.active_polygon_explorer_url.rstrip("/")
    return SimpleNamespace(
        tx_hash=tx_hash,
        timestamp=datetime.now(timezone.utc),
        block_number=None,
        network=settings.active_polygon_network_name,
        polygonscan_url=(f"{explorer}/tx/{tx_hash}" if tx_hash else None),
        gas_used=None,
    )


# ── DETERMINISTIC RECORD HASH (FIX #2) ──────────────────────────────────────

def _build_record_hash(record_type: str, record_id: str, record_data: Dict[str, Any]) -> str:
    """
    FIX #2: Build a deterministic SHA-256 hash from record content.

    Same record content always produces the same hash; the hash does NOT change with
    datetime.now() on re-notarization and is stable across restarts. For STR records
    (highest legal significance) it covers decision, rationale, dates, references.
    """

    if record_type == "str":
        content = {
            "record_type":       "str",
            "record_id":         record_id,
            "decision":          str(record_data.get("decision", "")),
            "decision_rationale": str(record_data.get("decision_rationale", "")),
            "decision_date":     (
                record_data["decision_date"].isoformat()
                if record_data.get("decision_date")
                else ""
            ),
            "stro_reference":    str(record_data.get("stro_reference", "") or ""),
            "trigger_type":      str(record_data.get("trigger_type", "")),
            "trigger_detail":    str(record_data.get("trigger_detail", "")),
            "client_id":         str(record_data.get("client_id", "") or ""),
            "csp_id":            str(record_data.get("csp_id", "")),
        }

    elif record_type == "cdd":
        content = {
            "record_type":   "cdd",
            "record_id":     record_id,
            "client_id":     str(record_data.get("client_id", "")),
            "csp_id":        str(record_data.get("csp_id", "")),
            "review_type":   str(record_data.get("review_type", "")),
            "status":        str(record_data.get("status", "")),
            "completed_by":  str(record_data.get("completed_by", "") or ""),
            "completed_at":  (
                record_data["completed_at"].isoformat()
                if record_data.get("completed_at")
                else ""
            ),
            "id_doc_type":   str(record_data.get("id_doc_type", "") or ""),
            "sanctions_clear": str(record_data.get("sanctions_clear", "")),
            "pep_result":    str(record_data.get("pep_result", "") or ""),
        }

    elif record_type in ("nominee_assessment", "nominee_director"):
        content = {
            "record_type":       record_type,
            "record_id":         record_id,
            "csp_id":            str(record_data.get("csp_id", "")),
            "client_id":         str(record_data.get("client_id", "")),
            "nominee_full_name": str(record_data.get("nominee_full_name", "")),
            "assessment_status": str(record_data.get("assessment_status", "")),
            "assessment_date":   (
                record_data["assessment_date"].isoformat()
                if record_data.get("assessment_date")
                else ""
            ),
            "assessed_by":       str(record_data.get("assessed_by", "") or ""),
            "assessment_outcome": str(record_data.get("assessment_outcome", "") or ""),
        }

    elif record_type == "training":
        content = {
            "record_type":    "training",
            "record_id":      record_id,
            "csp_id":         str(record_data.get("csp_id", "")),
            "staff_name":     str(record_data.get("staff_name", "")),
            "training_type":  str(record_data.get("training_type", "")),
            "training_title": str(record_data.get("training_title", "")),
            "provider":       str(record_data.get("provider", "")),
            "completion_date": (
                record_data["completion_date"].isoformat()
                if record_data.get("completion_date")
                else ""
            ),
            "status":         str(record_data.get("status", "")),
        }

    elif record_type == "aml_programme_approved":
        content = {
            "record_type": "aml_programme_approved",
            "record_id":   record_id,
            "csp_id":      str(record_data.get("csp_id", "")),
            "version":     str(record_data.get("version", "")),
            "approved_by": str(record_data.get("approved_by", "") or ""),
            "approved_at": (
                record_data["approved_at"].isoformat()
                if record_data.get("approved_at")
                else ""
            ),
            "pdf_hash":    str(record_data.get("pdf_hash", "") or ""),
        }

    else:
        # Generic fallback — use record_id + type only
        content = {
            "record_type": record_type,
            "record_id":   record_id,
            "csp_id":      str(record_data.get("csp_id", "")),
        }

    canonical = json.dumps(content, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _csp_owner_email(db, profile) -> Optional[str]:
    """Billing owner of the profile's org, falling back to the business email."""
    from app.core.models import CspOrganisation, User

    org = db.query(CspOrganisation).filter(
        CspOrganisation.id == profile.organisation_id
    ).first()
    if org and org.owner_user_id:
        user = db.query(User).filter(User.id == org.owner_user_id).first()
        if user and user.email:
            return user.email
    return profile.business_email or None


def _email_csp_documents(db, profile, attachments, generated_ok: int) -> bool:
    """Deliver the generated AML/CFT documents as attachments.

    Generation used to finish silently — the PDFs landed in S3 and the dashboard
    and nothing told the buyer. Attachments are the deliverable; the dashboard
    link is the durable copy (and the only place drafts can be attested).

    Never raises: the documents are already generated, notarized, and committed,
    so a mail failure must not trigger a retry that regenerates all of them.
    """
    from app.services.email_layout import branded_email_html, email_button
    from app.services.email_service import EmailService

    to_email = _csp_owner_email(db, profile)
    if not to_email:
        logger.error(
            "[CSPDocs] no recipient for profile %s — documents generated but undelivered",
            profile.id,
        )
        return False

    try:
        body_html = branded_email_html(
            f"""
            <p style="margin:0 0 4px;color:#64748b;text-transform:uppercase;letter-spacing:.1em;font-size:11px;">BOOPPA · CSP COMPLIANCE PACK</p>
            <h2 style="margin:0 0 12px;color:#0f172a;font-size:20px;">Your {generated_ok} AML/CFT documents are ready</h2>
            <p style="color:#334155;line-height:1.6;margin:0 0 16px;font-size:15px;">
              Generated for <strong>{profile.legal_name}</strong> (UEN {profile.uen}) from the
              profile you submitted, and attached to this email. Each document's SHA-256 hash
              is anchored on-chain, so you can prove the version you hold is the version issued.
            </p>
            <p style="color:#334155;line-height:1.6;margin:0 0 16px;font-size:15px;">
              <strong>These are drafts, and they are not yet your programme.</strong> ACRA expects
              an AML/CFT programme adopted by your firm, not a document a vendor wrote. Review each
              one, amend it to match how you actually operate, and attest to it in the dashboard —
              that attestation is what marks it in force.
            </p>
            {email_button("https://www.booppa.io/csp/dashboard", "Review and attest your documents")}
            """,
            title=f"Your CSP AML/CFT documents — {profile.legal_name}",
            preheader=f"{generated_ok} documents generated, attached, and anchored on-chain.",
        )
        sent = asyncio.run(EmailService().send_html_email(
            to_email=to_email,
            subject=f"Your {generated_ok} CSP AML/CFT documents are ready — {profile.legal_name}",
            body_html=body_html,
            attachments=attachments or None,
        ))
    except Exception as exc:
        logger.error("[CSPDocs] delivery email failed for %s: %s", to_email, exc)
        sent = False

    if not sent:
        try:
            from app.services.fulfillment import alert_payment_fulfillment_issue
            asyncio.run(alert_payment_fulfillment_issue(
                reason="CSP documents generated but delivery email failed",
                product_type="csp_pack",
                customer_email=to_email,
                extra={"profile_id": str(profile.id), "legal_name": profile.legal_name},
                notify_customer=False,
            ))
        except Exception:
            logger.exception("[CSPDocs] alert for failed delivery email also failed")
    return bool(sent)


# ── TASK 1: GENERATE CSP DOCUMENTS ───────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="csp.generate_documents",
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
    soft_time_limit=1200,
    time_limit=1260,
)
def generate_csp_documents(self, profile_id: str) -> dict:
    """
    Generate all AML/CFT/PF compliance documents via DeepSeek.
    Renders each to PDF, uploads to S3, and notarizes on-chain.
    """
    from app.core.models import CspProfile, CspClient, CspAmlProgramme, CspBlockchainEvidence
    from app.services.csp_doc_generator import generate_all_csp_documents, generate_csp_document_pdf

    db = _db()
    try:
        profile = db.query(CspProfile).filter(
            CspProfile.id == uuid.UUID(profile_id)
        ).first()
        if not profile:
            raise ValueError(f"CspProfile {profile_id} not found")

        clients = db.query(CspClient).filter(CspClient.csp_id == profile.id).all()

        profile_dict = {c.key: getattr(profile, c.key) for c in profile.__table__.columns}
        client_dicts = [
            {c.key: getattr(cl, c.key) for c in cl.__table__.columns}
            for cl in clients
        ]

        logger.info(
            "Generating CSP documents for %s (%d clients)",
            profile.legal_name, len(clients)
        )

        doc_results = generate_all_csp_documents(profile_dict, client_dicts)

        generated_ok = 0
        # (filename, bytes) for the completion email — the buyer gets the
        # documents in hand, not just a dashboard they have to remember to open.
        doc_attachments: list[tuple[str, bytes]] = []
        for dr in doc_results:
            if not dr.get("content"):
                logger.warning("Skipping failed doc: %s — %s", dr.get("doc_type"), dr.get("error"))
                continue
            generated_ok += 1

            doc_type = dr["doc_type"]
            content  = dr["content"]

            pdf_bytes, pdf_hash = generate_csp_document_pdf(
                title=dr["title"],
                body=content,
                meta={"legal_name": profile.legal_name, "uen": profile.uen, "doc_type": doc_type},
            )

            doc_attachments.append((
                f"{re.sub(r'[^A-Za-z0-9]+', '-', dr['title']).strip('-') or doc_type}-DRAFT.pdf",
                pdf_bytes,
            ))

            s3_key = f"csp/{profile_id}/documents/{doc_type}.pdf"
            try:
                _s3_upload(pdf_bytes, s3_key)
            except Exception as s3_err:
                logger.error("S3 upload failed for CSP doc %s: %s", doc_type, s3_err)

            bc_tx = bc_ts = bc_url = None
            try:
                notarization = _notarize_document_hash(
                    document_hash=pdf_hash,
                    metadata=f"doc:{doc_type}",
                    audit_id=profile_id,
                )
                bc_tx  = notarization.tx_hash
                bc_ts  = notarization.timestamp
                bc_url = notarization.polygonscan_url
            except Exception as bc_err:
                logger.error("Blockchain failed for CSP doc %s: %s", doc_type, bc_err)

            if doc_type == "aml_programme":
                latest = db.query(CspAmlProgramme).filter(
                    CspAmlProgramme.csp_id == profile.id
                ).order_by(CspAmlProgramme.version.desc()).first()
                version = (latest.version + 1) if latest else 1
                if latest:
                    latest.is_current = False

                prog = CspAmlProgramme(
                    csp_id=profile.id,
                    version=version,
                    is_current=True,
                    status="draft",
                    generated_by_model=dr.get("generated_by_model", "deepseek-chat"),
                    generation_cost_usd=dr.get("cost_usd"),
                    s3_key=s3_key,
                    pdf_hash=pdf_hash,
                    blockchain_tx_hash=bc_tx,
                    blockchain_timestamp=bc_ts,
                    polygonscan_url=bc_url,
                )
                db.add(prog)

            ev = CspBlockchainEvidence(
                csp_id=profile.id,
                record_type="csp_document",
                record_title=dr["title"],
                document_hash=pdf_hash,
                tx_hash=bc_tx or "pending",
                blockchain_timestamp=bc_ts,
                polygonscan_url=bc_url,
                metadata_payload=f"BOOPPA:CSP-DOC:{doc_type}:{pdf_hash[:16]}",
            )
            db.add(ev)

        # Guard against a silent empty success: if every document failed (e.g.
        # DEEPSEEK_API_KEY missing or the provider is down), do NOT mark the
        # programme as existing. Roll back, alert, and retry — never tell the
        # buyer their pack is ready when zero documents were produced.
        if generated_ok == 0:
            db.rollback()
            msg = (
                f"CSP document generation produced 0 documents for {profile.legal_name} "
                f"({profile_id}). Check DEEPSEEK_API_KEY / provider availability."
            )
            logger.error(msg)
            try:
                from app.services.fulfillment import alert_payment_fulfillment_issue
                asyncio.run(alert_payment_fulfillment_issue(
                    reason="CSP document generation produced 0 documents",
                    product_type="csp_pack",
                    customer_email=None,
                    extra={"profile_id": profile_id, "legal_name": profile.legal_name},
                    notify_customer=False,
                ))
            except Exception as alert_err:
                logger.error("Failed to send fulfillment alert: %s", alert_err)
            raise self.retry(
                exc=RuntimeError("CSP doc generation produced 0 documents"),
                countdown=60 * (self.request.retries + 1),
            )

        profile.aml_programme_exists = True
        db.commit()

        logger.info(
            "CSP document generation complete for %s — %d/%d docs",
            profile.legal_name, generated_ok, len(doc_results)
        )

        emailed = _email_csp_documents(db, profile, doc_attachments, generated_ok)

        return {
            "profile_id": profile_id,
            "docs_generated": generated_ok,
            "emailed": emailed,
        }

    except Exception as exc:
        logger.error("generate_csp_documents failed for %s: %s", profile_id, exc, exc_info=True)
        raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1))
    finally:
        db.close()


# ── TASK 2: NOTARIZE CSP RECORD (FIX #2) ─────────────────────────────────────

@celery_app.task(
    bind=True,
    name="csp.notarize_record",
    max_retries=5,
    default_retry_delay=30,
    acks_late=True,
)
def notarize_csp_record(self, record_id: str, record_type: str, profile_id: str) -> dict:
    """
    FIX #2: Notarize a CSP compliance record on-chain.

    Hash is built deterministically from record content (not datetime.now()), so
    re-running notarization produces an identical hash (idempotent) and ACRA/CAD can
    independently verify by re-hashing the record and comparing to the chain. For the
    v3 legal-layer records (tos_acceptance / programme_attestation / risk_classification)
    the router already computed a richer content_hash — prefer it when present.
    """
    from app.core.models import (
        CspCddRecord, CspStrReport, CspNomineeDirector,
        CspStaffTraining, CspAmlProgramme, CspBlockchainEvidence,
        CspTosAcceptance, CspProgrammeAttestation, CspRiskClassificationAudit,
    )

    db = _db()
    try:
        model_map = {
            "cdd":                    CspCddRecord,
            "str":                    CspStrReport,
            "nominee_assessment":     CspNomineeDirector,
            "nominee_director":       CspNomineeDirector,
            "training":               CspStaffTraining,
            "aml_programme_approved": CspAmlProgramme,
            # v3 legal-protection layer
            "tos_acceptance":         CspTosAcceptance,
            "programme_attestation":  CspProgrammeAttestation,
            "risk_classification":    CspRiskClassificationAudit,
        }
        model = model_map.get(record_type)
        if not model:
            raise ValueError(f"Unknown record_type: {record_type}")

        record = db.query(model).filter(model.id == uuid.UUID(record_id)).first()
        if not record:
            raise ValueError(f"Record {record_id} of type {record_type} not found")

        # ── BUILD DETERMINISTIC HASH FROM RECORD CONTENT ──────────────
        record_data = {c.key: getattr(record, c.key) for c in record.__table__.columns}
        document_hash = _build_record_hash(record_type, record_id, record_data)
        # Prefer the router-computed content hash for v3 legal records (richer content).
        if getattr(record, "content_hash", None):
            document_hash = record.content_hash

        logger.info(
            "Notarizing %s %s — content hash: %s",
            record_type, record_id, document_hash[:16]
        )

        notarization = _notarize_document_hash(
            document_hash=document_hash,
            metadata=record_type,
            audit_id=profile_id,
        )

        # Update record with blockchain data
        if hasattr(record, "blockchain_tx_hash"):
            record.blockchain_tx_hash = notarization.tx_hash
        if hasattr(record, "blockchain_timestamp"):
            record.blockchain_timestamp = notarization.timestamp
        if hasattr(record, "notarized_at"):
            record.notarized_at = notarization.timestamp
        if hasattr(record, "polygonscan_url"):
            record.polygonscan_url = notarization.polygonscan_url

        # Add to evidence ledger
        title_map = {
            "cdd":                    "CDD Completion Record",
            "str":                    "STR Decision Record",
            "nominee_assessment":     "Nominee F&P Assessment",
            "nominee_director":       "Nominee Director Record",
            "training":               "Staff Training Completion",
            "aml_programme_approved": "AML/CFT Programme Approval",
            "tos_acceptance":         "ToS Acceptance Attestation",
            "programme_attestation":  "Programme Approval Attestation",
            "risk_classification":    "Risk Classification Audit",
        }
        client_ref = ""
        if hasattr(record, "client_id") and record.client_id:
            client_ref = str(record.client_id)

        ev = CspBlockchainEvidence(
            csp_id=uuid.UUID(profile_id),
            record_type=record_type,
            record_id=uuid.UUID(record_id),
            record_title=title_map.get(record_type, record_type),
            related_client=client_ref,
            document_hash=document_hash,
            tx_hash=notarization.tx_hash or "pending",
            block_number=notarization.block_number,
            network=notarization.network,
            blockchain_timestamp=notarization.timestamp,
            polygonscan_url=notarization.polygonscan_url,
            gas_used=notarization.gas_used,
            metadata_payload=f"BOOPPA:CSP:{record_type.upper()}:{document_hash[:32]}",
        )
        db.add(ev)
        db.commit()

        logger.info(
            "Notarized %s %s → TX: %s | Hash: %s",
            record_type, record_id, notarization.tx_hash, document_hash[:16]
        )
        return {
            "record_id":     record_id,
            "record_type":   record_type,
            "document_hash": document_hash,
            "tx_hash":       notarization.tx_hash,
            "polygonscan":   notarization.polygonscan_url,
        }

    except Exception as exc:
        logger.error("notarize_csp_record failed for %s %s: %s", record_type, record_id, exc, exc_info=True)
        raise self.retry(exc=exc, countdown=30 * (self.request.retries + 1))
    finally:
        db.close()


# ── TASK 3: REFRESH SANCTIONS LISTS ──────────────────────────────────────────

@celery_app.task(name="csp.refresh_sanctions_lists")
def refresh_sanctions_lists_task() -> dict:
    """
    Refresh OFAC SDN and UN Consolidated sanctions list caches.
    Schedule via Celery Beat daily before the monitoring scan.
    """
    from app.services.csp_sanctions import refresh_sanctions_lists
    result = refresh_sanctions_lists()
    logger.info("Sanctions lists refreshed: %s", result)
    return result


# ── TASK 4: DAILY MONITORING ──────────────────────────────────────────────────

@celery_app.task(name="csp.daily_monitoring")
def csp_daily_monitoring() -> dict:
    """
    Daily compliance monitoring for all CSP profiles: flag expired CDD reviews,
    update calendar alert flags, quarterly sanctions re-screen for high-risk clients.
    """
    from app.core.models import CspProfile, CspClient, CspComplianceCalendar, RiskRating

    db = _db()
    try:
        now      = datetime.now(timezone.utc)
        profiles = db.query(CspProfile).filter(
            CspProfile.csp_pack_tier.isnot(None)
        ).all()

        alerts_sent    = 0
        cdd_expired    = 0
        rescreened     = 0

        for profile in profiles:
            clients = db.query(CspClient).filter(
                CspClient.csp_id == profile.id,
                CspClient.is_active == True,
            ).all()

            for client in clients:
                # Flag expired CDD
                if client.cdd_next_review and client.cdd_next_review < now:
                    if client.cdd_status == "completed":
                        client.cdd_status = "expired"
                        cdd_expired += 1
                        logger.info(
                            "CDD expired: client %s (%s) for CSP %s",
                            client.legal_name, client.id, profile.id
                        )

                # Quarterly sanctions re-screen for high-risk clients
                if client.risk_rating in (RiskRating.HIGH, RiskRating.VERY_HIGH):
                    last_screen = client.sanctions_screened_at
                    days_since  = (now - last_screen).days if last_screen else 999
                    if days_since >= 90:
                        try:
                            from app.services.csp_sanctions import (
                                screen_individual, screen_entity
                            )
                            fn = (
                                screen_individual
                                if client.client_type == "individual"
                                else screen_entity
                            )
                            result = fn(client.legal_name)
                            client.sanctions_screened    = True
                            client.sanctions_clear       = result.is_clear
                            client.sanctions_screened_at = now
                            client.sanctions_hits        = result.hits if result.hits else None
                            rescreened += 1

                            if not result.is_clear:
                                logger.warning(
                                    "SANCTIONS HIT on re-screen: %s (%s)",
                                    client.legal_name, client.id
                                )
                        except Exception as screen_err:
                            logger.error(
                                "Re-screen failed for %s: %s",
                                client.legal_name, screen_err
                            )

            # Update calendar alert flags
            cal_items = db.query(CspComplianceCalendar).filter(
                CspComplianceCalendar.csp_id == profile.id,
                CspComplianceCalendar.status == "pending",
            ).all()

            for item in cal_items:
                if not item.due_date:
                    continue
                days = (item.due_date - now).days
                if days < 0 and not item.alert_overdue_sent:
                    item.alert_overdue_sent = True
                    alerts_sent += 1
                    logger.warning(
                        "OVERDUE: [%s] %s for CSP %s",
                        item.pillar, item.title, profile.legal_name
                    )
                elif days <= 7 and not item.alert_7_days_sent:
                    item.alert_7_days_sent = True
                    alerts_sent += 1
                elif days <= 14 and not item.alert_14_days_sent:
                    item.alert_14_days_sent = True
                    alerts_sent += 1
                elif days <= 30 and not item.alert_30_days_sent:
                    item.alert_30_days_sent = True
                    alerts_sent += 1

        db.commit()
        logger.info(
            "Daily monitoring complete: %d profiles | %d CDD expired | "
            "%d re-screened | %d alerts flagged",
            len(profiles), cdd_expired, rescreened, alerts_sent
        )
        return {
            "profiles_scanned": len(profiles),
            "cdd_expired":      cdd_expired,
            "rescreened":       rescreened,
            "alerts_flagged":   alerts_sent,
        }

    finally:
        db.close()


# ── RUN SANCTIONS SCREENING TASK (async screening) ──────────────────────────

@celery_app.task(bind=True, name="csp.run_sanctions_screening", max_retries=3, default_retry_delay=30)
def run_sanctions_screening_task(
    self,
    record_id:    str,
    csp_id:       str,
    name_to_screen: str,
    client_type:  str,
    record_type:  str = "client",
):
    """
    Sanctions screening as an async Celery task (does not block the HTTP thread).

    record_type:
      "client" — updates CspClient (a hit escalates to VERY_HIGH + edd_required)
      "ubo"    — updates CspBeneficialOwner.is_sanctioned
    """
    db = _db()
    try:
        from app.services.csp_sanctions import screen_individual, screen_entity
        from app.core.models import CspClient, CspBeneficialOwner, RiskRating

        now = datetime.now(timezone.utc)
        fn  = screen_individual if client_type == "individual" else screen_entity

        try:
            result = fn(name_to_screen)
        except Exception as screen_exc:
            logger.error(
                "Sanctions screening failed for %s (%s): %s",
                name_to_screen, record_id, screen_exc
            )
            raise self.retry(exc=screen_exc)

        if record_type == "ubo":
            obj = db.query(CspBeneficialOwner).filter(
                CspBeneficialOwner.id == uuid.UUID(record_id)
            ).first()
            if obj:
                obj.is_sanctioned = not result.is_clear
                db.commit()
        else:
            client = db.query(CspClient).filter(
                CspClient.id == uuid.UUID(record_id)
            ).first()
            if client:
                client.sanctions_screened    = True
                client.sanctions_clear       = result.is_clear
                client.sanctions_screened_at = now
                client.sanctions_hits        = result.hits if result.hits else None

                if not result.is_clear and client.risk_rating not in (
                    RiskRating.HIGH, RiskRating.VERY_HIGH
                ):
                    client.risk_rating  = RiskRating.VERY_HIGH
                    client.edd_required = True
                    logger.warning(
                        "SANCTIONS HIT: client %s (%s) escalated to VERY_HIGH risk",
                        name_to_screen, record_id
                    )
                db.commit()

        return {
            "record_id":     record_id,
            "name_screened": name_to_screen,
            "is_clear":      result.is_clear,
            "hit_count":     result.hit_count,
            "hits":          result.hits,
            "lists_checked": result.lists_checked,
            "screened_at":   result.screened_at,
            "action_required": (
                f"SANCTIONS HIT on '{name_to_screen}': {result.hit_count} match(es). "
                "Consider declining service and filing an STR."
                if not result.is_clear else None
            ),
        }

    finally:
        db.close()


@celery_app.task(bind=True, name="csp.run_baseline", max_retries=2)
def run_csp_baseline_for_user(
    self,
    user_id: str,
    plan: str = "csp",
    billing_type: str = "one_time",
    override_company: Optional[str] = None,
    override_website: Optional[str] = None,
    bypass_idempotency: bool = False,
):
    """Generate + email the Day-1 CSP Registration Readiness Baseline.

    A CSP pack purchase used to deliver a two-line activation email with nothing
    attached (both the one-time and monthly paths — see csp_access.
    deliver_csp_activation, the only caller). The 8 AML/CFT documents still
    correctly wait for a CSP profile; this closes the gap where the buyer had
    nothing at all to open on day one.

    Emails ONE message covering both activation and the artifact, so the buyer
    doesn't receive two.
    """
    from app.core.models import Report, User
    from app.core.cache import cache as _cache
    from app.services.csp_baseline_generator import generate_csp_baseline_pdf
    from app.services.email_service import EmailService
    from app.services.email_layout import branded_email_html, email_button
    from app.services.evidence_enricher import display_legal_name, fetch_acra_status
    from app.services.storage import S3Service

    # Atomic once-only guard on the SEND, claimed just before the email rather
    # than at task entry: the body is wrapped in a broad retry, so an entry-time
    # claim would make every retry a no-op. Claiming late means a retry re-renders
    # and re-uploads (the S3 key is deterministic per user, so it overwrites
    # itself) but can still never send a second email.
    # `bypass_idempotency` is admin simulate-purchase harness only.
    _lock_key = f"csp_baseline_email_lock:{user_id}"

    db = _db()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user or not user.email:
            logger.warning("[CSPBaseline] no user/email for id=%s", user_id)
            return {"skipped": "no_user"}

        plan_label = (
            "CSP Monitoring Add-On" if plan == "csp_monitoring"
            else "CSP Compliance Pack — Full"
        )
        billing_label = (
            "One-time purchase" if billing_type == "one_time" else "Monthly subscription"
        )

        # Assessed entity is the CUSTOMER. Harness override first (so an admin
        # test checkout never stamps the real account's company onto a customer
        # document), then the resolved ACRA legal name.
        company_name = (override_company or "").strip() or display_legal_name(user, db)
        website = (override_website or "").strip() or (getattr(user, "website", "") or "")
        uen = (getattr(user, "uen", "") or "").strip() or None

        acra = {}
        try:
            acra = asyncio.run(fetch_acra_status(uen, company_name)) or {}
        except Exception as acra_err:
            # A registry outage must not cost the buyer their Day-1 artifact —
            # the generator renders an explicit "not confirmed" block instead.
            logger.warning("[CSPBaseline] ACRA lookup failed for %s: %s", user.email, acra_err)

        provisioning = [
            {"capability": "CSP compliance workspace", "status": "Active",
             "detail": f"{plan_label} active — open booppa.io/csp/dashboard"},
            {"capability": "Regulatory compliance calendar", "status": "Ready",
             "detail": "15 statutory deadlines seed automatically when you create your CSP profile"},
            {"capability": "AML/CFT document generation (8 documents)", "status": "Ready",
             "detail": "Queued the moment your CSP profile is submitted — issued as drafts for your attestation"},
            {"capability": "Sanctions screening (OFAC SDN + UN Consolidated)", "status": "Active",
             "detail": "Screen any client or UBO from the dashboard"},
            {"capability": "Blockchain evidence ledger", "status": "Active",
             "detail": "CDD, STR, and nominee assessment records are SHA-256 hashed and anchored on-chain"},
        ]

        pdf_bytes = generate_csp_baseline_pdf({
            "company_name": company_name,
            "website": website,
            "plan_label": plan_label,
            "billing_label": billing_label,
            "acra": acra,
            "provisioning": provisioning,
        })

        report_id = f"csp-baseline-{user.id}"
        file_key = f"reports/{report_id}.pdf"
        download_url = None
        try:
            download_url = asyncio.run(S3Service().upload_pdf(pdf_bytes, report_id))
        except Exception as up_err:
            logger.error("[CSPBaseline] S3 upload failed for %s: %s", user.email, up_err)

        if not download_url:
            raise RuntimeError("CSP baseline S3 upload produced no URL")

        # Persist so GET /csp/baseline/latest can re-serve this without the buyer
        # having to find the emailed link (presigns expire in 7 days).
        try:
            # The S3 key is deterministic per user, so a retry (or a re-purchase)
            # overwrites the object rather than adding one — update the existing
            # row in place instead of accumulating duplicate snapshots.
            existing = (
                db.query(Report)
                .filter(Report.owner_id == user.id, Report.file_key == file_key)
                .first()
            )
            snapshot = {
                "plan_label": plan_label,
                "billing_label": billing_label,
                "s3_url": download_url,
                "s3_key": file_key,
                "acra_found": bool(acra.get("found")),
                "uen": acra.get("uen") or uen,
            }
            if existing:
                existing.company_name = company_name
                existing.assessment_data = snapshot
                existing.status = "completed"
                existing.s3_url = download_url
                existing.completed_at = datetime.now(timezone.utc)
            else:
                db.add(Report(
                    owner_id=user.id,
                    framework="csp_baseline",
                    company_name=company_name,
                    assessment_data=snapshot,
                    status="completed",
                    s3_url=download_url,
                    file_key=file_key,
                    completed_at=datetime.now(timezone.utc),
                ))
            db.commit()
        except Exception as persist_err:
            logger.warning("[CSPBaseline] snapshot persist failed for %s: %s", user.email, persist_err)
            db.rollback()

        if not bypass_idempotency and not _cache.add(_lock_key, {"sent": True}, ttl=86400):
            logger.info("[CSPBaseline] Idempotency drop: baseline already emailed to %s", user_id)
            return {"skipped": "idempotent", "download_url": download_url}

        entity_line = (
            f"We've confirmed <strong>{acra.get('registered_name') or company_name}</strong>"
            f" (UEN {acra.get('uen')}) against the ACRA register"
            if acra.get("found") and acra.get("uen")
            else f"We've prepared your baseline for <strong>{company_name}</strong>"
        )
        body_html = branded_email_html(
            f"""
            <p style="margin:0 0 4px;color:#64748b;text-transform:uppercase;letter-spacing:.1em;font-size:11px;">BOOPPA · {plan_label}</p>
            <h2 style="margin:0 0 12px;color:#0f172a;font-size:20px;">Your {plan_label} is active — baseline ready</h2>
            <p style="color:#334155;line-height:1.6;margin:0 0 16px;font-size:15px;">
              {entity_line}, and recorded what your purchase has initialised.
            </p>
            {email_button(download_url, "Download your CSP Readiness Baseline (PDF)")}
            <p style="color:#334155;line-height:1.6;margin:16px 0 0;font-size:15px;">
              <strong>Next:</strong> sign in, accept the Terms of Service, and create your CSP
              profile. That submission is what generates your eight AML/CFT documents — they
              can't be written before we know your business, and they're issued as drafts for
              your attestation.
            </p>
            <p style="color:#64748b;font-size:13px;margin:12px 0 0;line-height:1.6;">
              Open your <a href="https://www.booppa.io/csp/dashboard" style="color:#10b981;">CSP dashboard</a> to begin.
            </p>
            """,
            title=f"Your {plan_label} is active",
            preheader=f"CSP Registration Readiness Baseline for {company_name}.",
        )
        # Attached AND linked: the attachment is the deliverable in hand, the S3
        # link survives a forwarded mail losing its attachment (and re-presigns
        # via GET /csp/baseline/latest once the 7-day URL expires).
        safe_name = re.sub(r"[^A-Za-z0-9]+", "-", company_name).strip("-") or "entity"
        sent = asyncio.run(EmailService().send_html_email(
            to_email=user.email,
            subject=f"Your CSP Readiness Baseline — {plan_label}",
            body_html=body_html,
            attachments=[(f"CSP-Readiness-Baseline-{safe_name}.pdf", pdf_bytes)],
        ))
        if not sent:
            logger.error("[CSPBaseline] delivery email rejected for %s", user.email)
            from app.services.fulfillment.helpers import _alert_payment_fulfillment_issue
            asyncio.run(_alert_payment_fulfillment_issue(
                reason="CSP activated and baseline generated but delivery email rejected by provider",
                product_type=f"csp:{plan}:{billing_type}",
                customer_email=user.email,
                session_id=report_id,
            ))
        else:
            logger.info("[CSPBaseline] Delivered baseline to %s", user.email)
        return {"user_id": str(user.id), "download_url": download_url, "emailed": bool(sent)}
    except Exception as exc:
        logger.error("[CSPBaseline] Failed for %s: %s", user_id, exc)
        raise self.retry(exc=exc, countdown=120)
    finally:
        db.close()
