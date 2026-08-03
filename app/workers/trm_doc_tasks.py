"""MAS TRM Suite — document pack generation task.

The Suite's baseline tracker (`tasks.run_suite_trm_baseline_for_user`) records
*that* 13 domains exist; this task produces the artefacts a MAS-regulated entity
must actually hold on file — governance policy, FSM-N06 cyber hygiene
attestation, outsourcing risk register, incident response plan, BCM/DR plan,
IT audit & VAPT log, access control policy.

Kept out of `app/workers/tasks.py` (already ~10k lines), parallel to
`csp_tasks.py`, and registered via the Celery `include` list.

Three properties this file exists to preserve:

  * **Post-payment retries go through `_retry_or_alert`.** A bare `self.retry`
    that exhausts is a silently undelivered SGD 1,800–4,500/mo purchase — Celery
    logs MaxRetriesExceededError and nothing reaches a human.
  * **No email unless the completeness gate passes.** `evaluate_pack_gate`
    requires every expected document to be both generated AND delivered. The CSP
    bug that shipped 3-of-8 packs passed a `generated_ok > 0` check.
  * **Unknown licence blocks the register only.** Status becomes
    `blocked_licence_unknown`, the other six are delivered, and the customer is
    asked to confirm. A bank handed a non-bank register is worse than no
    register: it is signed evidence the entity misread its own regime.

Generated ONCE on Suite activation. Not per board-report cycle — seven DeepSeek
calls per month per customer buys nothing when the underlying controls have not
moved. Regeneration is explicit: a licence correction re-enqueues with
`only_doc_types=["outsourcing_risk_register"]`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# Deliberately distinct from `trm_baseline_email_lock:{user_id}` — a baseline
# resend must not suppress the pack, and vice versa.
PACK_LOCK_KEY = "trm_doc_pack_lock:{user_id}"
PACK_LOCK_TTL = 86400


def _fetch_control_dicts(db, org) -> List[Dict[str, Any]]:
    """Control rows in the dict shape `build_trm_context` expects.

    Mirrors `tasks.run_suite_trm_baseline_for_user._fetch_controls` (minus the
    per-evidence detail the tracker renders and the pack does not need), so the
    pack and the tracker are built from the same view of the programme and
    cannot disagree about what is documented vs tested.
    """
    from app.core.models import TrmControl, TrmEvidence
    from app.services.trm_sector_override import reorder_controls_by_sector

    if not org:
        return []
    rows = db.query(TrmControl).filter(TrmControl.organisation_id == org.id).all()
    rows = reorder_controls_by_sector(rows, getattr(org, "sector", None))

    ev_by_control: Dict[Any, list] = {}
    ev_rows = (
        db.query(TrmEvidence)
        .filter(TrmEvidence.control_id.in_([r.id for r in rows] or [None]))
        .order_by(TrmEvidence.uploaded_at.asc())
        .all()
    )
    for ev in ev_rows:
        ev_by_control.setdefault(ev.control_id, []).append(ev)

    out: List[Dict[str, Any]] = []
    for r in rows:
        evs = ev_by_control.get(r.id, [])
        tested = [e for e in evs if (e.evidence_type or "documented") == "tested"]
        latest_tested = max((e.tested_at for e in tested if e.tested_at), default=None)
        out.append({
            "domain": r.domain,
            "control_ref": r.control_ref,
            "status": r.status,
            "risk_rating": r.risk_rating,
            "gap_analysis": r.gap_analysis,
            "evidence_count": len(evs),
            "tested_count": len(tested),
            "latest_tested_at": (
                latest_tested.strftime("%d %b %Y") if latest_tested else None
            ),
        })
    return out


def _resolve_branding(db, user, org, plan: str) -> Optional[Dict[str, Any]]:
    """Pro Suite white-label branding, via the same path the board report uses.

    Standard Suite receives the identical seven documents — the tier difference
    is presentational only. Withholding a statutorily required artefact from a
    SGD 1,800/mo customer is not a defensible upsell.
    """
    if plan != "pro_suite" or org is None:
        return None
    try:
        from app.billing.enforcement import PRO_SUITE_PLAN_KEYS
        from app.core.models import WhiteLabelConfig
        from app.white_label_and_sso import resolve_branding_for_owner

        cfg = (
            db.query(WhiteLabelConfig)
            .filter(WhiteLabelConfig.organisation_id == org.id)
            .first()
        )
        if not cfg:
            return None
        return resolve_branding_for_owner(
            user.id, db, plan_keys=PRO_SUITE_PLAN_KEYS,
            fallback_header=getattr(user, "company", None),
        )
    except Exception as exc:  # noqa: BLE001 — branding is cosmetic, never fatal
        logger.warning("[TRMPack] branding resolve failed for %s: %s", user.id, exc)
        return None


@celery_app.task(
    bind=True,
    name="trm.generate_document_pack",
    max_retries=3,
    default_retry_delay=120,
    acks_late=True,
    soft_time_limit=1800,
    time_limit=1860,
    queue="heavy_queue",
)
def generate_trm_document_pack(
    self,
    user_id: str,
    override_company: Optional[str] = None,
    bypass_idempotency: bool = False,
    only_doc_types: Optional[List[str]] = None,
):
    """Generate, anchor, persist and deliver the MAS TRM document pack.

    Seven DeepSeek calls at ~60s each plus per-document retries — hence the
    1800s soft limit (CSP's 1200 is too tight for this catalog).

    `only_doc_types` regenerates a subset. It also bypasses the idempotency
    lock's purpose (the pack already went out), so the caller on that path must
    pass `bypass_idempotency=True` — that is the licence-correction flow.
    """
    from app.core.cache import cache
    from app.core.db import SessionLocal
    from app.core.models import Organisation, TrmDocumentPack, User
    from app.services import trm_pack_delivery as delivery
    from app.services.mas_licence import outsourcing_regime
    from app.services.trm_doc_generator import (
        build_trm_context,
        generate_all_trm_documents,
    )
    from app.workers.tasks import _retry_or_alert

    lock_key = PACK_LOCK_KEY.format(user_id=user_id)
    if not bypass_idempotency and not cache.add(lock_key, {"queued": True}, ttl=PACK_LOCK_TTL):
        logger.info("[TRMPack] idempotency drop: pack already generated for %s", user_id)
        return {"status": "skipped_idempotent"}

    db = SessionLocal()
    pack = None
    customer_email = None
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user or not user.email:
            logger.warning("[TRMPack] no user/email for id=%s", user_id)
            return {"status": "no_user"}
        customer_email = user.email

        org = (
            db.query(Organisation)
            .filter(Organisation.owner_user_id == user.id)
            .order_by(Organisation.created_at.asc())
            .first()
        )
        plan = getattr(user, "plan", "") or ""
        controls = _fetch_control_dicts(db, org)
        ctx = build_trm_context(org, user, controls, db, override_company=override_company)

        # Regime is resolved once, here, and snapshotted onto the row — the
        # register in this pack was branched on THIS value, and the org's value
        # can legitimately change afterwards.
        regime = outsourcing_regime(ctx.licence_type) if ctx.licence_known else None

        pack = TrmDocumentPack(
            id=uuid.uuid4(),
            organisation_id=(org.id if org else None),
            owner_user_id=user.id,
            plan_label=ctx.plan_label,
            mas_licence_type=ctx.licence_type,
            outsourcing_regime=regime,
            status="generating",
        )
        if pack.organisation_id is None:
            # organisation_id is NOT NULL: a Suite subscriber with no org row is
            # a broken activation, not a pack to half-write.
            logger.error("[TRMPack] no organisation for user %s — cannot generate", user_id)
            raise RuntimeError("no organisation for Suite subscriber")
        db.add(pack)
        db.commit()

        logger.info(
            "[TRMPack] pack=%s user=%s licence=%s regime=%s subset=%s",
            pack.id, user_id, ctx.licence_type, regime, only_doc_types,
        )

        results = generate_all_trm_documents(ctx, only_doc_types=only_doc_types)

        pack.status = "building_pdfs"
        db.commit()

        branding = _resolve_branding(db, user, org, plan)
        documents = delivery.build_pack_documents(
            results, ctx, pack.organisation_id, pack.id, branding=branding,
        )

        status, missing = delivery.evaluate_pack_gate(
            documents, ctx.licence_type, only_doc_types=only_doc_types,
        )

        pack.documents = delivery.strip_bytes(documents)
        pack.total_cost_usd = round(
            sum(float(r.get("cost_usd") or 0) for r in results), 5
        )
        pack.master_hash = delivery.master_hash_for(documents)
        if pack.master_hash:
            _mtx, _ = delivery.anchor_document(
                pack.master_hash, f"BOOPPA:TRM-PACK:{str(pack.id)[:8]}"
            )
            pack.master_tx_hash = _mtx  # None on failure — never the literal "pending"
        pack.status = status
        db.commit()

        if status == delivery.PACK_STATUS_ERROR:
            # Gate failed: nothing is emailed. Raising routes into
            # _retry_or_alert below, which retries and then alerts ops — a
            # partial pack must never reach the customer as if it were complete.
            raise RuntimeError(
                f"TRM pack completeness gate failed — missing: {', '.join(missing)}"
            )

        delivery.persist_report_snapshot(db, pack, ctx, documents)

        sent = delivery.email_pack(
            user.email, ctx, documents,
            status=status, outsourcing_regime_value=regime,
            is_correction=only_doc_types is not None,
        )
        # "delivered is not completed": the pack is only complete once the mail
        # provider accepted it. send_html_email returns bool and does not raise.
        pack.completed_at = datetime.now(timezone.utc)
        if not sent:
            pack.error_message = "generated and stored, but delivery email was rejected"
        db.commit()

        logger.info(
            "[TRMPack] ✓ pack=%s status=%s docs=%d emailed=%s cost=$%.4f",
            pack.id, status,
            sum(1 for e in documents.values() if e.get("url")),
            sent, pack.total_cost_usd or 0,
        )
        return {
            "pack_id": str(pack.id),
            "status": status,
            "outsourcing_regime": regime,
            "emailed": sent,
            "documents": sorted(k for k, v in documents.items() if v.get("url")),
        }

    except Exception as exc:  # noqa: BLE001
        logger.error("[TRMPack] generation failed for %s: %s", user_id, exc, exc_info=True)
        try:
            if pack is not None:
                pack.status = "error"
                pack.error_message = f"{type(exc).__name__}: {exc}"[:1000]
                db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        _retry_or_alert(
            self, exc,
            product_type="trm_document_pack",
            customer_email=customer_email,
            extra={"user_id": str(user_id), "pack_id": str(pack.id) if pack else None},
        )
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass
