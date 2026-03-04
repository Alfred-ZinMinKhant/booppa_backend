"""
Notarization Elevation Service — Dual Silent Standard
======================================================
Manages NotarizationMetadata records for vendors who have completed
at least one notarization.

HARD ISOLATION RULES:
  ✗ Does NOT read or write VendorScore or ScoreSnapshot
  ✗ Does NOT modify procurement ordering
  ✗ Does NOT trigger billing or plan gates
  ✓ Only reads from Proof (notarization proxy) and VerifyRecord
  ✓ Only writes to NotarizationMetadata

StructuralLevel:
  STANDARD  → no NotarizationMetadata row for this vendor
  ELEVATED  → has a NotarizationMetadata row

VerificationDepthNEL (separate from VendorStatusEngine depth):
  BASIC      1  completed notarization
  ENHANCED   2–3
  DEEP       4–5 or 1+ ready evidence packages
  ENTERPRISE 6+  or 3+ ready evidence packages
"""

import hashlib
import logging
from datetime import datetime
from typing import Optional
from sqlalchemy.orm import Session

from app.core.models_v8 import NotarizationMetadata, EvidencePackage
from app.core.models import VerifyRecord, Proof

logger = logging.getLogger(__name__)

ELEVATION_LOGIC_VERSION = "1.0"
ELEVATION_MIN_NOTARIZATIONS = 1

DEPTH_THRESHOLDS = {
    "ENHANCED_MIN":   2,
    "DEEP_MIN":       4,
    "ENTERPRISE_MIN": 6,
    "DEEP_WITH_EP":   1,   # READY evidence packages
    "ENTERPRISE_WITH_EP": 3,
}


# ── Pure helpers ──────────────────────────────────────────────────────────────

def derive_verification_depth(notal_count: int, ep_count: int) -> str:
    """Pure: derive VerificationDepthNEL from counts. No DB calls."""
    if (
        notal_count >= DEPTH_THRESHOLDS["ENTERPRISE_MIN"]
        or ep_count >= DEPTH_THRESHOLDS["ENTERPRISE_WITH_EP"]
    ):
        return "ENTERPRISE"
    if (
        notal_count >= DEPTH_THRESHOLDS["DEEP_MIN"]
        or ep_count >= DEPTH_THRESHOLDS["DEEP_WITH_EP"]
    ):
        return "DEEP"
    if notal_count >= DEPTH_THRESHOLDS["ENHANCED_MIN"]:
        return "ENHANCED"
    return "BASIC"


def compute_confidence_score(compliance_score: int, depth_score: int, total_evidence: int) -> int:
    """
    0–100 composite:
      complianceScore × 0.5  (0–50)
      + depthScore × 10      (0–50: 5 levels × 10)
      + min(evidence × 3, 30)  (0–30, capped)
    """
    raw = (
        compliance_score * 0.5
        + depth_score * 10
        + min(total_evidence * 3, 30)
    )
    return min(round(raw), 100)


def generate_canonical_validation_id(vendor_id: str, notarization_hash: str) -> str:
    """SHA-256(vendorId:notarizationHash)[:32] — stable audit identifier."""
    payload = f"{vendor_id}:{notarization_hash}"
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def generate_public_hash(vendor_id: str, notarized_at: datetime, validation_id: str) -> str:
    """SHA-256(vendorId + notarized_at ISO + validationId) — independently verifiable."""
    payload = f"{vendor_id}:{notarized_at.isoformat()}:{validation_id}"
    return hashlib.sha256(payload.encode()).hexdigest()


def _nel_depth_score(depth: str) -> int:
    """Map VerificationDepthNEL to 0-5 numeric scale for confidenceScore."""
    return {"BASIC": 1, "ENHANCED": 2, "DEEP": 3, "ENTERPRISE": 4}.get(depth, 1)


# ── Service functions ─────────────────────────────────────────────────────────

def create_or_update_elevation(db: Session, vendor_id: str) -> Optional[dict]:
    """
    Called after a Proof record is created (notarization complete).
    Upserts NotarizationMetadata for the vendor.
    Returns the elevation dict, or None if vendor does not yet qualify.
    """
    # Count COMPLETE notarizations — using Proof as the notarization record
    notarizations = db.query(Proof).join(VerifyRecord).filter(
        VerifyRecord.vendor_id == vendor_id,
    ).all()

    notal_count = len(notarizations)
    if notal_count < ELEVATION_MIN_NOTARIZATIONS:
        return None  # STANDARD by absence

    # Count READY EvidencePackage rows for this vendor
    ep_rows = db.query(EvidencePackage).filter(
        EvidencePackage.vendor_id == vendor_id,
        EvidencePackage.status == "READY",
    ).all()
    ep_count = len(ep_rows)
    total_evidence = notal_count + ep_count

    # complianceScore for confidenceScore
    verify = db.query(VerifyRecord).filter(VerifyRecord.vendor_id == vendor_id).first()
    compliance_score = verify.compliance_score if verify else 0

    # Use most recent notarization for validation_id
    latest_notarization = sorted(notarizations, key=lambda n: n.created_at, reverse=True)[0]
    latest_hash = latest_notarization.hash_value or str(latest_notarization.id)
    notarized_at = latest_notarization.created_at

    # Build sector-scoped evidence counts:
    # notarizations attributed equally across all registered sectors,
    # READY evidence packages attributed to their specific sector.
    from app.core.models import VendorSector
    primary_sectors = db.query(VendorSector).filter(
        VendorSector.vendor_id == vendor_id
    ).all()

    # Sector-level ep breakdown
    ep_by_sector: dict = {}
    for ep in ep_rows:
        if ep.sector:
            ep_by_sector[ep.sector] = ep_by_sector.get(ep.sector, 0) + 1

    evidence_by_sector: dict = {}
    for sector_row in primary_sectors:
        s = sector_row.sector
        evidence_by_sector[s] = notal_count + ep_by_sector.get(s, 0)

    depth = derive_verification_depth(notal_count, ep_count)
    depth_score = _nel_depth_score(depth)
    confidence = compute_confidence_score(compliance_score, depth_score, total_evidence)
    validation_id = generate_canonical_validation_id(vendor_id, latest_hash)
    public_hash = generate_public_hash(vendor_id, notarized_at, validation_id)

    existing = db.query(NotarizationMetadata).filter(
        NotarizationMetadata.vendor_id == vendor_id
    ).first()

    if existing:
        existing.notarized_at              = notarized_at
        existing.validation_id             = validation_id
        existing.verification_depth        = depth
        existing.structural_level          = "ELEVATED"
        existing.public_hash               = public_hash
        existing.logic_version             = ELEVATION_LOGIC_VERSION
        existing.evidence_count            = total_evidence
        existing.evidence_count_by_sector  = evidence_by_sector
        existing.confidence_score          = float(confidence)
    else:
        existing = NotarizationMetadata(
            vendor_id                 = vendor_id,
            notarized_at              = notarized_at,
            validation_id             = validation_id,
            verification_depth        = depth,
            structural_level          = "ELEVATED",
            public_hash               = public_hash,
            logic_version             = ELEVATION_LOGIC_VERSION,
            evidence_count            = total_evidence,
            evidence_count_by_sector  = evidence_by_sector,
            confidence_score          = float(confidence),
        )
        db.add(existing)

    db.commit()
    db.refresh(existing)
    logger.info(f"Elevation upserted for vendor={vendor_id}, depth={depth}, confidence={confidence}")

    return _row_to_dict(existing)


def fetch_elevation_metadata(db: Session, vendor_id: str) -> dict:
    """Single-vendor DTO. Returns STANDARD shell when no row found."""
    row = db.query(NotarizationMetadata).filter(
        NotarizationMetadata.vendor_id == vendor_id
    ).first()
    if not row:
        return _standard_shell()
    return _row_to_dict(row)


def fetch_elevation_metadata_batch(db: Session, vendor_ids: list) -> dict:
    """
    Batch variant for procurement list query.
    Returns {vendor_id: elevation_dict}. Missing keys → STANDARD shell.
    """
    if not vendor_ids:
        return {}

    rows = db.query(NotarizationMetadata).filter(
        NotarizationMetadata.vendor_id.in_(vendor_ids)
    ).all()

    result = {str(r.vendor_id): _row_to_dict(r) for r in rows}
    for vid in vendor_ids:
        if str(vid) not in result:
            result[str(vid)] = _standard_shell()
    return result


# ── Private helpers ───────────────────────────────────────────────────────────

def _row_to_dict(row: NotarizationMetadata) -> dict:
    return {
        "structural_level":          row.structural_level,
        "verification_depth":        row.verification_depth,
        "notarized_at":              row.notarized_at.isoformat() if row.notarized_at else None,
        "validation_id":             row.validation_id,
        "public_hash":               row.public_hash,
        "logic_version":             row.logic_version,
        "evidence_count":            row.evidence_count,
        "evidence_count_by_sector":  row.evidence_count_by_sector or {},
        "confidence_score":          row.confidence_score,
    }


def _standard_shell() -> dict:
    return {
        "structural_level":          "STANDARD",
        "verification_depth":        None,
        "notarized_at":              None,
        "validation_id":             None,
        "public_hash":               None,
        "logic_version":             ELEVATION_LOGIC_VERSION,
        "evidence_count":            0,
        "evidence_count_by_sector":  {},
        "confidence_score":          0.0,
    }
