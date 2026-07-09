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
        return {"profile_id": profile_id, "docs_generated": generated_ok}

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
