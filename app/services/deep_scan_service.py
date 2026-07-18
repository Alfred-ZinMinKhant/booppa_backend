"""
Deep Scan Service — Phase 3
===========================
The real build behind the "Deep Scan — 11-dimension PDPA + certifications +
financial risk" claim that Buyer Professional/Enterprise sells.

Reuses existing machinery — nothing new is scraped that we don't already
collect elsewhere:

  * `evidence_enricher.fetch_acra_status`      — entity status / age (financial-risk input)
  * `evidence_enricher.fetch_pdpc_enforcement` — published enforcement (breach + financial-risk input)
  * `evidence_enricher.fetch_ssl_grade`        — TLS posture (security dimension)
  * `evidence_enricher.fetch_hosting_signals`  — hosting region (cross-border dimension)
  * `evidence_enricher.extract_website_signals`— certs / policy completeness / residency
  * `pdpa_free_scan_service.run_free_scan`     — live header/cookie/body scan (security signals)
  * `GebizAwardHistory`                        — award recency / count (financial-risk input)

Everything is graded from **public disclosure** — an absent published signal is
scored as "not publicly disclosed", never asserted as a private fact. No real
third-party company name is ever attached to a fabricated negative signal; the
only entity graded is the vendor actually being scanned, using real registry data.

Output feeds `/procurement/snapshot/{slug}` and persists to
`DeepScanDimensionHistory` for Phase-4 Deep-Scan drift detection.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

_BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; BooppaBot/1.0; +https://booppa.io)"}
_FETCH_TIMEOUT = 15.0


def _classify(score: int) -> str:
    if score >= 85:
        return "Compliant"
    if score >= 50:
        return "Partial"
    return "Non-Compliant"


def _dim(name: str, score: int, detail: dict | None = None, category: str = "pdpa") -> dict[str, Any]:
    score = max(0, min(100, int(score)))
    return {
        "category": category,
        "dimension_name": name,
        "status": _classify(score),
        "score": score,
        "detail": detail or {},
    }


async def _fetch_site_text(website: str) -> tuple[str, Optional[str]]:
    """Best-effort fetch of the homepage HTML. Returns (text, final_url)."""
    if not website:
        return "", None
    url = website if website.startswith("http") else f"https://{website}"
    try:
        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT, follow_redirects=True, verify=False  # nosec B501
        ) as client:
            resp = await client.get(url, headers=_BROWSER_HEADERS)
            return (resp.text or "")[:200_000], str(resp.url)
    except Exception as e:  # noqa: BLE001 — network best-effort
        logger.info("Deep Scan site fetch failed for %s: %s", url, e)
        return "", None


# ── PDPA dimensions (11) ──────────────────────────────────────────────────────

def _pdpa_dimensions(sig: dict, ssl: dict, hosting: dict, pdpc: dict, free_scan: dict) -> list[dict]:
    """Grade 11 PDPA obligations from collected public signals.

    `sig` = extract_website_signals output (may be {"available": False}).
    """
    avail = bool(sig.get("available"))
    out: list[dict] = []

    def s(flag: str) -> bool:
        return bool(sig.get(flag)) if avail else False

    # 1. Openness — Privacy Notice published (§20)
    published = avail and (s("pdpa_mentioned") or s("dpo_mentioned") or s("retention_policy_mentioned"))
    out.append(_dim("Privacy Notice Published (§20 Openness)", 90 if published else 25,
                    {"pdpa_mentioned": s("pdpa_mentioned")}))

    # 2. DPO Designated & Published (§11(3))
    out.append(_dim("DPO Designated (§11(3))", 92 if s("dpo_mentioned") else 30,
                    {"dpo_mentioned": s("dpo_mentioned")}))

    # 3. Consent Obligation (§13/14) — cookie/marketing consent posture
    cookie_findings = [f for f in (free_scan.get("findings") or []) if "cookie" in (f.get("title", "").lower())]
    if cookie_findings:
        consent_score = 35
    elif s("pdpa_mentioned"):
        consent_score = 80
    else:
        consent_score = 45
    out.append(_dim("Consent Obligation (§13/14)", consent_score,
                    {"cookie_flags": len(cookie_findings)}))

    # 4. Access & Correction (§21/22)
    out.append(_dim("Access & Correction (§21/22)", 85 if s("subprocessors_mentioned") or s("dpa_mentioned") else 40,
                    {"dpa_mentioned": s("dpa_mentioned")}))

    # 5. Accuracy Obligation (§23) — heuristic: policy completeness proxy
    completeness = sum(1 for f in (
        "retention_policy_mentioned", "breach_policy_mentioned",
        "subprocessors_mentioned", "dpa_mentioned",
    ) if s(f))
    out.append(_dim("Accuracy & Governance (§23)", 55 + completeness * 10,
                    {"policy_signals": completeness}))

    # 6. Protection Obligation (§24) — TLS + encryption + live header scan
    grade = (ssl.get("grade") or "").upper()
    if grade in ("A+", "A", "A-"):
        prot = 95
    elif grade in ("B", "C"):
        prot = 60
    elif grade:
        prot = 35
    elif s("encryption_generic") or s("tls_mentioned"):
        prot = 70
    else:
        prot = 45
    hi_sev = [f for f in (free_scan.get("findings") or []) if (f.get("severity", "").lower() in ("high", "critical"))]
    prot -= min(30, 10 * len(hi_sev))
    out.append(_dim("Protection Obligation (§24)", prot,
                    {"ssl_grade": grade or None, "high_findings": len(hi_sev)}))

    # 7. Retention Limitation (§25)
    out.append(_dim("Retention Limitation (§25)", 88 if s("retention_policy_mentioned") else 35,
                    {"retention_policy_mentioned": s("retention_policy_mentioned")}))

    # 8. Transfer Limitation (§26) — hosting region / stated residency
    region = hosting.get("inferred_region")
    if region == "Singapore" or s("singapore_residency_mentioned"):
        xfer = 90
    elif sig.get("non_sg_regions_mentioned"):
        xfer = 50
    elif hosting.get("inferred_provider"):
        xfer = 65
    else:
        xfer = 55
    out.append(_dim("Transfer Limitation (§26)", xfer,
                    {"inferred_region": region, "residency_stated": s("singapore_residency_mentioned")}))

    # 9. Data Breach Notification (§26A-D) — policy + PDPC enforcement history
    if pdpc.get("checked") and pdpc.get("found"):
        breach = 10
    elif s("breach_policy_mentioned"):
        breach = 90
    else:
        breach = 40
    out.append(_dim("Data Breach Notification (§26A-D)", breach,
                    {"pdpc_enforcement_found": bool(pdpc.get("found")),
                     "breach_policy_mentioned": s("breach_policy_mentioned")}))

    # 10. Sub-Processor / DPA Disclosure (Accountability)
    out.append(_dim("Sub-Processor & DPA Disclosure", 88 if s("subprocessors_mentioned") and s("dpa_mentioned")
                    else (65 if (s("subprocessors_mentioned") or s("dpa_mentioned")) else 35),
                    {"subprocessors": s("subprocessors_mentioned"), "dpa": s("dpa_mentioned")}))

    # 11. NRIC / National Identifier Handling — PDPC record as negative signal
    nric_flag = pdpc.get("checked") and pdpc.get("found")
    out.append(_dim("NRIC / Identifier Handling", 50 if nric_flag else 80,
                    {"pdpc_signal": bool(nric_flag)}))

    return out


# ── Certifications dimension ──────────────────────────────────────────────────

def _certifications_dimension(sig: dict) -> dict:
    avail = bool(sig.get("available"))
    certs = {
        "ISO 27001": bool(sig.get("iso_27001_mentioned")) if avail else False,
        "ISO 27017": bool(sig.get("iso_27017_mentioned")) if avail else False,
        "ISO 27018": bool(sig.get("iso_27018_mentioned")) if avail else False,
        "ISO 27701": bool(sig.get("iso_27701_mentioned")) if avail else False,
        "SOC 2": bool(sig.get("soc_2_mentioned")) if avail else False,
        "PCI-DSS": bool(sig.get("pci_dss_mentioned")) if avail else False,
    }
    held = [k for k, v in certs.items() if v]
    # ISO 27001 or SOC 2 is the anchor; supplementary certs add confidence.
    if certs["ISO 27001"] or certs["SOC 2"]:
        score = 90 + min(8, 2 * (len(held) - 1))
    elif held:
        score = 70
    else:
        score = 25
    return _dim(
        "Certifications (ISO/SOC/PCI)", score,
        {"held": held, "iso_27001_year": sig.get("iso_27001_year")},
        category="certifications",
    )


# ── Financial-risk dimension ──────────────────────────────────────────────────

def _financial_risk_dimension(db, vendor, acra: dict, pdpc: dict) -> dict:
    """Defensible free financial-risk heuristic from data already ingested:
    ACRA entity status + age, GeBIZ award history/recency, PDPC fines.
    Bureau-grade data (CreditSafe/ACRA iShop) is a deferred paid add-on."""
    from app.core.models import GebizAwardHistory

    score = 60  # neutral baseline
    detail: dict[str, Any] = {}

    # ACRA entity status — the single strongest signal.
    if acra.get("found"):
        if acra.get("live"):
            score += 15
        else:
            score -= 35
        detail["acra_status"] = acra.get("entity_status")
        # Entity age from registration date.
        reg = acra.get("registration_date")
        if reg:
            try:
                year = int(str(reg)[:4])
                age = datetime.now(timezone.utc).year - year
                detail["entity_age_years"] = age
                if age >= 5:
                    score += 10
                elif age < 2:
                    score -= 5
            except (ValueError, TypeError):
                pass
    else:
        score -= 10
        detail["acra_status"] = "not_found"

    # GeBIZ award history — recent public awards indicate a going concern.
    try:
        name = (getattr(vendor, "company", "") or "").strip()
        if name:
            rows = (
                db.query(GebizAwardHistory)
                .filter(GebizAwardHistory.supplier_name.ilike(f"%{name}%"))
                .order_by(GebizAwardHistory.awarded_date.desc())
                .limit(50)
                .all()
            )
            detail["gebiz_award_count"] = len(rows)
            if rows:
                score += min(12, 3 * len(rows))
                latest = rows[0].awarded_date
                if latest:
                    days = (datetime.now(timezone.utc).date() - latest).days
                    detail["days_since_last_award"] = days
                    if days <= 730:
                        score += 5
    except Exception as e:  # noqa: BLE001
        logger.info("Deep Scan GeBIZ lookup failed: %s", e)

    # PDPC enforcement fines depress financial standing (regulatory exposure).
    if pdpc.get("checked") and pdpc.get("found"):
        score -= 15
        detail["pdpc_enforcement"] = True

    return _dim("Financial Risk Assessment", score, detail, category="financial_risk")


# ── Orchestration ─────────────────────────────────────────────────────────────

async def run_deep_scan(db, vendor) -> dict[str, Any]:
    """Run a full Deep Scan for a vendor `User` row and persist dimensions.

    Returns a summary dict; the caller (Celery task / endpoint) decides how to
    surface it. Never raises on individual signal-fetch failure.
    """
    from app.services.evidence_enricher import (
        fetch_acra_status, fetch_pdpc_enforcement, fetch_ssl_grade,
        fetch_hosting_signals, extract_website_signals,
    )

    website = getattr(vendor, "website", None) or ""
    company = getattr(vendor, "company", None) or ""
    uen = getattr(vendor, "uen", None)

    site_text, _final = await _fetch_site_text(website)

    # Live header/cookie/body scan (sync) — reuse the free scanner.
    free_scan: dict = {}
    if website:
        try:
            from app.services.pdpa_free_scan_service import run_free_scan
            free_scan = run_free_scan(website) or {}
        except Exception as e:  # noqa: BLE001
            logger.info("Deep Scan free-scan failed: %s", e)

    # External signals — gather independently; any failure degrades gracefully.
    acra = pdpc = ssl = hosting = {}
    try:
        acra = await fetch_acra_status(uen=uen, company_name=company)
    except Exception as e:  # noqa: BLE001
        logger.info("Deep Scan ACRA failed: %s", e)
    try:
        pdpc = await fetch_pdpc_enforcement(company_name=company, uen=uen)
    except Exception as e:  # noqa: BLE001
        logger.info("Deep Scan PDPC failed: %s", e)
    try:
        ssl = await fetch_ssl_grade(website) if website else {}
    except Exception as e:  # noqa: BLE001
        logger.info("Deep Scan SSL failed: %s", e)
    try:
        hosting = await fetch_hosting_signals(website) if website else {}
    except Exception as e:  # noqa: BLE001
        logger.info("Deep Scan hosting failed: %s", e)

    sig = extract_website_signals(site_text) if site_text else {"available": False}

    dimensions = _pdpa_dimensions(sig, ssl, hosting, pdpc, free_scan)
    dimensions.append(_certifications_dimension(sig))
    dimensions.append(_financial_risk_dimension(db, vendor, acra, pdpc))

    scan_id = uuid.uuid4()
    now = datetime.utcnow()
    _persist_dimensions(db, vendor.id, scan_id, dimensions, now)

    pdpa_dims = [d for d in dimensions if d["category"] == "pdpa"]
    overall = round(sum(d["score"] for d in pdpa_dims) / len(pdpa_dims)) if pdpa_dims else 0

    return {
        "scan_id": str(scan_id),
        "vendor_id": str(vendor.id),
        "company": company,
        "overall_pdpa_score": overall,
        "dimensions": dimensions,
        "certifications": next((d for d in dimensions if d["category"] == "certifications"), None),
        "financial_risk": next((d for d in dimensions if d["category"] == "financial_risk"), None),
        "generated_at": now.replace(tzinfo=timezone.utc).isoformat(),
    }


def _persist_dimensions(db, vendor_id, scan_id, dimensions: list[dict], captured_at) -> None:
    from app.core.models import DeepScanDimensionHistory
    for d in dimensions:
        db.add(DeepScanDimensionHistory(
            vendor_id=vendor_id,
            scan_id=scan_id,
            category=d["category"],
            dimension_name=d["dimension_name"],
            status=d["status"],
            score=d["score"],
            detail=d.get("detail") or {},
            captured_at=captured_at,
        ))
    db.commit()


def _load_scan_dims(db, vendor_id, scan_id) -> list[dict[str, Any]]:
    from app.core.models import DeepScanDimensionHistory
    rows = (
        db.query(DeepScanDimensionHistory)
        .filter(
            DeepScanDimensionHistory.vendor_id == vendor_id,
            DeepScanDimensionHistory.scan_id == scan_id,
        )
        .all()
    )
    return [{
        "category": r.category,
        "dimension_name": r.dimension_name,
        "status": r.status,
        "score": r.score,
    } for r in rows]


def deep_scan_drift_for_vendor(db, vendor_id) -> Optional[dict[str, Any]]:
    """Diff a vendor's two most recent Deep Scans for worsened dimensions.

    Returns None when there aren't two distinct scans to compare, or when
    nothing worsened. Otherwise returns:
      {"current_scan_id", "previous_scan_id", "worsened": [ {dimension_name,
       previous_status, current_status, previous_score, current_score}, ... ]}

    Reuses `diff_snapshots` — the Deep-Scan dimension shape (dimension_name /
    status / score) matches the PDPA snapshot shape exactly.
    """
    from app.core.models import DeepScanDimensionHistory

    scan_rows = (
        db.query(DeepScanDimensionHistory.scan_id, DeepScanDimensionHistory.captured_at)
        .filter(DeepScanDimensionHistory.vendor_id == vendor_id)
        .order_by(DeepScanDimensionHistory.captured_at.desc())
        .all()
    )
    # Collapse to distinct scan_ids in captured order (a scan writes many rows).
    ordered_scans: list = []
    seen: set = set()
    for sid, _ts in scan_rows:
        if sid in seen:
            continue
        seen.add(sid)
        ordered_scans.append(sid)
        if len(ordered_scans) == 2:
            break
    if len(ordered_scans) < 2:
        return None

    current_id, previous_id = ordered_scans[0], ordered_scans[1]
    from app.services.pdpa_dimension_snapshot import diff_snapshots
    worsened = diff_snapshots(
        _load_scan_dims(db, vendor_id, previous_id),
        _load_scan_dims(db, vendor_id, current_id),
    )
    if not worsened:
        return None
    return {
        "current_scan_id": str(current_id),
        "previous_scan_id": str(previous_id),
        "worsened": worsened,
    }


def latest_deep_scan(db, vendor_id) -> Optional[dict[str, Any]]:
    """Return the most recent persisted Deep Scan for a vendor, or None."""
    from app.core.models import DeepScanDimensionHistory
    latest_row = (
        db.query(DeepScanDimensionHistory)
        .filter(DeepScanDimensionHistory.vendor_id == vendor_id)
        .order_by(DeepScanDimensionHistory.captured_at.desc())
        .first()
    )
    if not latest_row:
        return None
    rows = (
        db.query(DeepScanDimensionHistory)
        .filter(
            DeepScanDimensionHistory.vendor_id == vendor_id,
            DeepScanDimensionHistory.scan_id == latest_row.scan_id,
        )
        .all()
    )
    dims = [{
        "category": r.category,
        "dimension_name": r.dimension_name,
        "status": r.status,
        "score": r.score,
        "detail": r.detail or {},
    } for r in rows]
    pdpa_dims = [d for d in dims if d["category"] == "pdpa"]
    overall = round(sum(d["score"] for d in pdpa_dims) / len(pdpa_dims)) if pdpa_dims else 0
    return {
        "scan_id": str(latest_row.scan_id),
        "overall_pdpa_score": overall,
        "dimensions": dims,
        "certifications": next((d for d in dims if d["category"] == "certifications"), None),
        "financial_risk": next((d for d in dims if d["category"] == "financial_risk"), None),
        "generated_at": latest_row.captured_at.replace(tzinfo=timezone.utc).isoformat(),
    }
