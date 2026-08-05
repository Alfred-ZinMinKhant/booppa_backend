"""MAS TRM document pack — PDF rendering, S3, anchoring, gate, delivery.

This is the layer between `trm_doc_generator` (which produces text/JSON) and the
Celery task (Phase 6, `app/workers/trm_doc_tasks.py`). It is deliberately a
plain module of functions taking an explicit `db` so it can be driven from the
demo harness and from tests without a broker.

Three properties are load-bearing and should survive refactors:

1. **S3 keys are pack-scoped** — `trm/{org}/packs/{pack_id}/{doc_type}.pdf`.
   CSP writes `csp/{profile}/documents/{doc_type}.pdf` and overwrites on every
   run. Here a register regenerated after a licence correction must not destroy
   the copy the customer may already have printed and signed; which regime each
   delivered artefact asserted is exactly the fact an inspection turns on.

2. **The completeness gate is generated AND delivered.** A document that
   generated but failed to upload has no link in the email, so the customer
   receives a pack that silently omits it. `generated_ok > 0` is not a gate —
   that is the check the CSP `co_argcount` bug walked straight through while
   shipping 3-of-8 packs.

3. **Anchor failure is non-fatal, but never fabricated.** A missing anchor
   stores `tx_hash=None`. Never the literal "pending": every downstream reader
   that does not route through `is_real_onchain_tx` will render it as proof.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.services.compliance_doc_pdf import (
    TRM_DRAFT_DISCLAIMER,
    render_compliance_document,
    render_register_table,
)
from app.services.trm_doc_generator import (
    TRM_DOC_SCHEMA_VERSION,
    TRM_DOCUMENT_CATALOG,
    TrmDocContext,
    expected_doc_types,
)

logger = logging.getLogger(__name__)

PACK_STATUS_READY = "ready"
PACK_STATUS_BLOCKED = "blocked_licence_unknown"
PACK_STATUS_ERROR = "error"

DASHBOARD_URL = "https://www.booppa.io/vendor/trm"

_REGIME_LABEL = {
    "bank_notices": "MAS Notice 658 / 1121 (binding on banks and merchant banks)",
    "non_bank_guidelines": (
        "MAS Guidelines on Outsourcing (Financial Institutions other than Banks) "
        "— guidance, not a binding notice"
    ),
}


# ── PDF rendering ────────────────────────────────────────────────────────────

def _slug(text: str, fallback: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-") or fallback


def render_pack_document(
    result: Dict[str, Any],
    ctx: TrmDocContext,
    *,
    branding: Optional[Dict[str, Any]] = None,
) -> Tuple[bytes, str]:
    """Render one generator result to a PDF. Returns (pdf_bytes, sha256_of_pdf).

    Text documents render their Markdown body. JSON documents (the Outsourcing
    Register, the VAPT log) render header + notes as the body and the rows as a
    real table via `render_register_table`, using the caller-side column set the
    generator pinned — not whatever keys the model happened to emit.
    """
    meta = {
        "legal_name": ctx.legal_name,
        "uen": ctx.uen,
        "licence_label": ctx.licence_label,
        "doc_type": result["doc_type"],
        "schema_version": result.get("schema_version", TRM_DOC_SCHEMA_VERSION),
        "verification_critical": bool(result.get("verification_critical")),
        "branding": branding,
    }

    content = result["content"]
    extra_flow: Optional[list] = None

    if result.get("mode") == "json" and isinstance(content, dict):
        columns = list(content.get("columns") or [])
        rows = list(content.get("rows") or [])
        body_parts: List[str] = []
        if content.get("header"):
            body_parts.append(str(content["header"]))
        if content.get("instrument"):
            body_parts.append(f"**Governing instrument:** {content['instrument']}")
        notes = content.get("notes") or []
        if notes:
            body_parts.append("## Completion and maintenance notes")
            body_parts.extend(f"- {n}" for n in notes)
        body = "\n\n".join(body_parts)
        extra_flow = render_register_table(rows, columns)
    else:
        body = str(content)

    return render_compliance_document(
        result["title"],
        body,
        meta,
        header_label="MAS TRM DOCUMENT PACK",
        disclaimer=TRM_DRAFT_DISCLAIMER["not_certification"],
        draft_banner=True,
        signature_block=True,
        extra_flow=extra_flow,
    )


# ── S3 + anchoring ───────────────────────────────────────────────────────────

def pack_s3_key(organisation_id: Any, pack_id: Any, doc_type: str) -> str:
    return f"trm/{organisation_id}/packs/{pack_id}/{doc_type}.pdf"


def upload_pack_pdf(pdf_bytes: bytes, key: str) -> str:
    """Upload and return a 7-day presigned URL. Raises on failure (the caller
    records the document as undelivered, which the gate then catches)."""
    from app.services.storage import S3Service

    svc = S3Service()
    svc.s3_client.put_object(
        Bucket=svc.bucket,
        Key=key,
        Body=pdf_bytes,
        ContentType="application/pdf",
        ServerSideEncryption="AES256",
    )
    return svc.s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": svc.bucket, "Key": key},
        ExpiresIn=604800,
    )


def anchor_document(sha256_hex: str, metadata: str) -> Tuple[Optional[str], Optional[str]]:
    """Anchor a hash on-chain. Returns (tx_hash, explorer_url), (None, None) on
    failure — anchoring is evidence-grade nice-to-have, not a delivery blocker."""
    from app.core.config import settings
    from app.services.blockchain import BlockchainService

    try:
        tx = asyncio.run(
            BlockchainService().anchor_evidence(sha256_hex, metadata=metadata, force=False)
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("[TRMPack] anchor failed for %s: %s", metadata, exc)
        return None, None
    if not tx:
        return None, None
    explorer = settings.active_polygon_explorer_url.rstrip("/")
    return tx, f"{explorer}/tx/{tx}"


def master_hash_for(documents: Dict[str, Dict[str, Any]]) -> str:
    """Deterministic hash over the per-document hashes, sorted by doc_type, so
    re-computing it from a delivered pack reproduces the anchored value."""
    joined = "|".join(
        f"{k}:{v.get('sha256') or ''}" for k, v in sorted(documents.items())
    )
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# ── build + gate ─────────────────────────────────────────────────────────────

def build_pack_documents(
    doc_results: List[Dict[str, Any]],
    ctx: TrmDocContext,
    organisation_id: Any,
    pack_id: Any,
    *,
    branding: Optional[Dict[str, Any]] = None,
    anchor: bool = True,
) -> Dict[str, Dict[str, Any]]:
    """Render, upload and anchor every successfully generated document.

    Skipped documents (licence-gated) are recorded with their skip_reason so the
    pack row explains itself; they are not treated as failures.
    """
    documents: Dict[str, Dict[str, Any]] = {}

    for result in doc_results:
        doc_type = result["doc_type"]
        entry: Dict[str, Any] = {
            "title": result.get("title"),
            "word_count": result.get("word_count"),
            "cost_usd": result.get("cost_usd"),
            "schema_version": result.get("schema_version", TRM_DOC_SCHEMA_VERSION),
            "s3_key": None,
            "url": None,
            "sha256": None,
            "tx_hash": None,
            "polygonscan_url": None,
            "error": result.get("error"),
        }
        if result.get("outsourcing_regime"):
            entry["outsourcing_regime"] = result["outsourcing_regime"]

        if result.get("skipped"):
            entry["skipped"] = True
            entry["skip_reason"] = result.get("skip_reason")
            documents[doc_type] = entry
            continue

        if result.get("content") is None:
            documents[doc_type] = entry
            continue

        try:
            pdf_bytes, pdf_hash = render_pack_document(result, ctx, branding=branding)
        except Exception as exc:  # noqa: BLE001
            logger.error("[TRMPack] render failed for %s: %s", doc_type, exc, exc_info=True)
            entry["error"] = f"render failed: {exc}"
            documents[doc_type] = entry
            continue

        entry["sha256"] = pdf_hash
        entry["pdf_bytes"] = pdf_bytes  # stripped before persistence
        entry["filename"] = f"{_slug(result.get('title', ''), doc_type)}-DRAFT.pdf"

        key = pack_s3_key(organisation_id, pack_id, doc_type)
        try:
            entry["url"] = upload_pack_pdf(pdf_bytes, key)
            entry["s3_key"] = key
        except Exception as exc:  # noqa: BLE001
            logger.error("[TRMPack] S3 upload failed for %s: %s", doc_type, exc)
            entry["error"] = f"upload failed: {exc}"

        if anchor:
            tx, url = anchor_document(
                pdf_hash, metadata=f"BOOPPA:TRM-DOC:{doc_type}:{pdf_hash[:16]}"
            )
            entry["tx_hash"] = tx
            entry["polygonscan_url"] = url

        documents[doc_type] = entry

    return documents


def evaluate_pack_gate(
    documents: Dict[str, Dict[str, Any]],
    licence_type: Optional[str],
    only_doc_types: Optional[List[str]] = None,
) -> Tuple[str, List[str]]:
    """Returns (status, missing_doc_types).

    A document counts only if it both generated and has a download URL. Whatever
    the licence class cannot support is not *expected* — the pack is `blocked`,
    not `error`: the real documents this entity's regime supports, plus an
    explicit ask for the rest, is an honest outcome; a guessed regime is not.

    `blocked` is keyed off the resolvable set, not off `licence_type is None`.
    An MPI resolves its cyber-hygiene Notice (FSM-N14) but has no mapped
    technology-risk Notice, so it is partially gated even with a licence set,
    and reporting that pack as `ready` would hide three missing documents.

    `only_doc_types` narrows what "complete" means to the subset that was asked
    for — the licence-correction path regenerates only the newly-resolvable
    documents, and without this the gate would report the ones it never
    attempted as missing, fail the run, and withhold what the customer is
    waiting for.
    """
    expected = expected_doc_types(licence_type)
    if only_doc_types is not None:
        subset = set(only_doc_types)
        expected = tuple(dt for dt in expected if dt in subset)
    missing = [
        dt for dt in expected
        if not (documents.get(dt) or {}).get("url")
    ]
    if missing:
        return PACK_STATUS_ERROR, missing
    if len(expected_doc_types(licence_type)) < len(TRM_DOCUMENT_CATALOG):
        return PACK_STATUS_BLOCKED, []
    return PACK_STATUS_READY, []


def strip_bytes(documents: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """JSONB-safe copy — PDF bytes live in S3, not in Postgres."""
    return {
        k: {kk: vv for kk, vv in v.items() if kk != "pdf_bytes"}
        for k, v in documents.items()
    }


# ── delivery ─────────────────────────────────────────────────────────────────

def _document_list_html(documents: Dict[str, Dict[str, Any]]) -> str:
    rows = []
    for doc_type, entry in documents.items():
        if not entry.get("url"):
            continue
        title = entry.get("title") or doc_type
        rows.append(
            f'<li style="margin:0 0 8px;">'
            f'<a href="{entry["url"]}" style="color:#0f766e;">{title}</a>'
            f"</li>"
        )
    return "<ul style=\"padding-left:18px;margin:0 0 16px;color:#334155;font-size:15px;\">" + "".join(rows) + "</ul>"


def email_pack(
    to_email: str,
    ctx: TrmDocContext,
    documents: Dict[str, Dict[str, Any]],
    *,
    status: str,
    outsourcing_regime_value: Optional[str],
    is_correction: bool = False,
) -> bool:
    """Deliver download links (not attachments — seven policy PDFs blow past
    provider attachment limits, and CSP only gets away with it by accident).

    The body states the entity, UEN, licence class and **which outsourcing
    regime was applied**, so a wrong licence selection is catchable by the
    customer before they sign anything.
    """
    from app.services.email_layout import branded_email_html, email_button
    from app.services.email_service import EmailService

    delivered = sum(1 for e in documents.values() if e.get("url"))
    regime_line = _REGIME_LABEL.get(outsourcing_regime_value or "", None)

    # A correction run delivers a subset. Calling that "your MAS TRM document
    # pack is ready" a second time reads as a duplicate of the original mail and
    # buries the one thing that actually changed.
    if is_correction:
        # Name what actually regenerated. Confirming a licence can unlock one
        # document or five depending on the class, so a fixed "your Outsourcing
        # Risk Register is ready" is wrong for most licence classes.
        titles = [
            (e.get("title") or dt)
            for dt, e in documents.items() if e.get("url")
        ]
        noun = "document" if delivered == 1 else "documents"
        only = f" {titles[0]}" if delivered == 1 else ""
        heading = (
            f"Your{only} is ready" if delivered == 1
            else f"{delivered} more MAS TRM documents are ready"
        )
        subject = (
            f"Your MAS TRM{only} is ready — {ctx.legal_name}" if delivered == 1
            else f"{delivered} more MAS TRM documents are ready — {ctx.legal_name}"
        )
        intro = (
            "You confirmed your MAS licence class, so we generated the "
            f"{noun} that {'was' if delivered == 1 else 'were'} withheld — we "
            "now know which MAS Notice and outsourcing regime bind you. "
            "Documents from your original pack are unchanged and remain in "
            "your workspace."
        )
    else:
        heading = f"Your {delivered} MAS TRM documents are ready"
        subject = f"Your MAS TRM document pack is ready — {ctx.legal_name}"
        intro = None

    if status == PACK_STATUS_BLOCKED:
        resolvable = set(expected_doc_types(ctx.licence_type))
        withheld = [
            s.title for s in TRM_DOCUMENT_CATALOG if s.doc_type not in resolvable
        ]
        withheld_html = "".join(
            f'<li style="margin:0 0 4px;">{t}</li>' for t in withheld
        )
        gate_html = f"""
        <div style="border-left:4px solid #b45309;background:#fffbeb;padding:12px 16px;margin:0 0 16px;">
          <p style="margin:0 0 6px;color:#92400e;font-weight:700;font-size:14px;">
            {len(withheld)} document{'' if len(withheld) == 1 else 's'} not generated
          </p>
          <ul style="margin:0 0 8px;padding-left:18px;color:#78350f;font-size:14px;">
            {withheld_html}
          </ul>
          <p style="margin:0;color:#78350f;line-height:1.6;font-size:14px;">
            Your MAS licence class is not confirmed on your account, and these
            documents cannot be written without it. The MAS Notice that binds you
            differs by licence class — a bank is bound by FSM-N05/N06, a merchant
            bank by FSM-N11/N12, an insurer by FSM-N03/N04, a payment licensee by
            FSM-N14 — and the outsourcing regime differs too (MAS Notices 658 /
            1121, binding since 11 December 2024, for banks and merchant banks;
            the Guidelines on Outsourcing for every other licensed FI). We will
            not guess: a signed attestation citing the wrong Notice is evidence
            that the entity misread its own obligations. Confirm your licence
            class and we will generate what it unlocks.
          </p>
        </div>
        {email_button(DASHBOARD_URL, "Confirm your MAS licence class")}
        """
    else:
        gate_html = f"""
        <p style="color:#334155;line-height:1.6;margin:0 0 16px;font-size:15px;">
          <strong>Outsourcing regime applied:</strong> {regime_line or 'n/a'}.
          If that is not the regime that binds you, correct your licence class and
          regenerate the register <em>before</em> anyone signs it.
        </p>
        {email_button(DASHBOARD_URL, "Open your TRM workspace")}
        """

    body_html = branded_email_html(
        f"""
        <p style="margin:0 0 4px;color:#64748b;text-transform:uppercase;letter-spacing:.1em;font-size:11px;">BOOPPA · MAS TRM SUITE</p>
        <h2 style="margin:0 0 12px;color:#0f172a;font-size:20px;">{heading}</h2>
        {f'<p style="color:#334155;line-height:1.6;margin:0 0 16px;font-size:15px;">{intro}</p>' if intro else ''}
        <p style="color:#334155;line-height:1.6;margin:0 0 16px;font-size:15px;">
          Generated for <strong>{ctx.legal_name}</strong>{f' (UEN {ctx.uen})' if ctx.uen else ''},
          licence class <strong>{ctx.licence_label}</strong>. Each document's SHA-256 hash is
          anchored on-chain, so you can prove the version you hold is the version issued.
        </p>
        <p style="color:#334155;line-height:1.6;margin:0 0 16px;font-size:15px;">
          <strong>These are AI-generated drafts and they are not yet your programme.</strong>
          MAS expects a technology risk programme adopted by your board, not a document a
          vendor wrote. Review each one, correct it to match how you actually operate, and
          have your authorised representative sign it. Until signed, none of them carries
          evidentiary value — do not present them to MAS in unsigned form.
        </p>
        {_document_list_html(documents)}
        {gate_html}
        <p style="color:#64748b;line-height:1.6;margin:16px 0 0;font-size:13px;">
          Links expire in 7 days; your workspace holds the durable copies. Booppa does not
          certify compliance with the MAS Technology Risk Management Guidelines or any MAS
          Notice — the Guidelines are guidance, the Notices are binding, and only your
          entity can attest to either.
        </p>
        """,
        title=f"Your MAS TRM document pack — {ctx.legal_name}",
        preheader=f"{delivered} documents generated and anchored on-chain.",
    )

    try:
        sent = asyncio.run(EmailService().send_html_email(
            to_email=to_email,
            subject=subject,
            body_html=body_html,
        ))
    except Exception as exc:  # noqa: BLE001
        logger.error("[TRMPack] delivery email raised for %s: %s", to_email, exc)
        sent = False

    if not sent:
        try:
            from app.services.fulfillment import alert_payment_fulfillment_issue
            asyncio.run(alert_payment_fulfillment_issue(
                reason="MAS TRM document pack generated but delivery email failed",
                product_type="trm_document_pack",
                customer_email=to_email,
                extra={"legal_name": ctx.legal_name, "delivered": delivered},
                notify_customer=False,
            ))
        except Exception:
            logger.exception("[TRMPack] alert for failed delivery email also failed")
    return bool(sent)


def persist_report_snapshot(db, pack, ctx: TrmDocContext, documents: Dict[str, Dict[str, Any]]) -> None:
    """Write a Report(framework="trm_document_pack") so existing TRM listing
    surfaces pick the pack up without a frontend change."""
    from app.core.models import Report

    primary = documents.get("trm_governance_policy") or {}
    try:
        db.add(Report(
            owner_id=pack.owner_user_id,
            framework="trm_document_pack",
            company_name=ctx.legal_name,
            assessment_data={
                "pack_id": str(pack.id),
                "plan_label": pack.plan_label,
                "mas_licence_type": pack.mas_licence_type,
                "outsourcing_regime": pack.outsourcing_regime,
                "schema_version": TRM_DOC_SCHEMA_VERSION,
                "documents": {
                    k: {kk: v.get(kk) for kk in ("title", "s3_key", "url", "sha256", "tx_hash")}
                    for k, v in documents.items()
                },
            },
            status="completed",
            s3_url=primary.get("url"),
            file_key=primary.get("s3_key"),
            completed_at=datetime.now(timezone.utc),
        ))
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TRMPack] snapshot persist failed for pack %s: %s", pack.id, exc)
        db.rollback()
