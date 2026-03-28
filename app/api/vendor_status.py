"""
Vendor Status Routes — V8
==========================
GET /api/vendor/status           → full VendorStatusProfile (auth'd vendor)
GET /api/vendor/sector-pressure  → sector competitive pressure snapshot + message
GET /api/vendor/dashboard-cal    → CAL payload: ladder + suggestion + message + sectorPressure
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.db import get_db, get_current_user
from app.services.vendor_status import get_vendor_status
from app.services.sector_pressure import (
    get_sector_competitive_pressure,
    generate_sector_pressure_message,
    get_cached_rows,
    count_recently_active,
)
from app.services.cal import (
    analyze_activation_gaps,
    generate_upgrade_suggestion,
    render_message,
)
from app.services.notarization_elevation import fetch_elevation_metadata
from app.core.models import VendorSector

router = APIRouter()


@router.get("/status")
async def vendor_status(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Full VendorStatusProfile for the authenticated vendor.
    Derived from trust facts only — no payment data.
    """
    vendor_id = str(current_user.id)
    status    = get_vendor_status(db, vendor_id)
    return status


@router.get("/sector-pressure")
async def sector_pressure(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Returns the vendor's competitive snapshot in their primary sector,
    plus an informational message.
    Read-only — no ranking modification.
    """
    vendor_id = str(current_user.id)

    # Resolve primary sector (1 DB query)
    sector_row = db.query(VendorSector).filter(
        VendorSector.vendor_id == current_user.id
    ).first()

    if not sector_row:
        raise HTTPException(status_code=404, detail="No sector registered for this vendor.")

    primary_sector = sector_row.sector

    snapshot          = get_sector_competitive_pressure(db, primary_sector, vendor_id)
    cached_rows       = get_cached_rows(primary_sector)
    recently_active   = count_recently_active(cached_rows, 30)
    message           = generate_sector_pressure_message(snapshot, recently_active)

    return {"snapshot": snapshot, "message": message}


@router.get("/dashboard-cal")
async def dashboard_cal(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Commercial Activation Layer (CAL) payload for the vendor dashboard:
      - activation ladder (which levels are met, what is next)
      - upgrade suggestion (probability score + insight)
      - dynamic message (gap × sector density × tier matrix)
      - sector pressure context
    """
    vendor_id = str(current_user.id)

    # Single DB lookup: sector + elevation
    sector_row = db.query(VendorSector).filter(
        VendorSector.vendor_id == current_user.id
    ).first()

    if not sector_row:
        raise HTTPException(status_code=404, detail="No sector registered for this vendor.")

    primary_sector = sector_row.sector

    # Elevation metadata & vendor score data
    elevation = fetch_elevation_metadata(db, vendor_id)

    from app.core.models import VendorScore
    score_row = db.query(VendorScore).filter(
        VendorScore.vendor_id == current_user.id
    ).first()

    vendor_snapshot = {
        "vendorId":        vendor_id,
        "compliance_score":  score_row.compliance_score if score_row else 0,
        "evidence_count":    elevation.get("evidence_count", 0),
        "confidence_score":  elevation.get("confidence_score", 0.0),
        "is_elevated":       elevation.get("structural_level") == "ELEVATED",
        "plan":              getattr(current_user, "role", "VENDOR"),
        "tier":              "STANDARD",
    }

    # Sector pressure (cache-first, at most 1 DB query)
    sector_pressure = get_sector_competitive_pressure(db, primary_sector, vendor_id)
    cached_rows     = get_cached_rows(primary_sector)
    recently_active = count_recently_active(cached_rows, 30)

    # Peer evidence top-3 avg
    peer_evidences = sorted(
        [
            r.get("evidence_count", 0)
            for r in cached_rows
            if str(r.get("vendor_id")) != vendor_id
        ],
        reverse=True,
    )
    top3 = peer_evidences[:3]
    top3_avg = round(sum(top3) / len(top3), 1) if top3 else 0.0

    # CAL pure functions
    gap_analysis = analyze_activation_gaps(vendor_snapshot, sector_pressure)
    suggestion   = generate_upgrade_suggestion(
        vendor_snapshot,
        gap_analysis,
        sector_pressure["totalElevated"],
    )
    message = render_message({
        "vendor":               vendor_snapshot,
        "sector":               primary_sector,
        "peerAvgEvidence":      sector_pressure["avgEvidence"],
        "vendorEvidence":       sector_pressure["vendorEvidence"],
        "top3AvgEvidence":      top3_avg,
        "recentlyActiveCount":  recently_active,
        "totalElevatedPeers":   sector_pressure["totalElevated"],
        "gapAnalysis":          gap_analysis,
        "suggestion":           suggestion,
    })

    return {
        "ladder":         gap_analysis,
        "suggestion":     suggestion,
        "message":        message,
        "sectorPressure": sector_pressure,
    }


# ── Evidence Management ───────────────────────────────────────────────────────

from fastapi import UploadFile, File
from app.core.models import Proof, VerifyRecord, LifecycleStatus, VerificationLevel
import hashlib

@router.get("/evidence")
async def list_evidence(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """List all notarized evidence for the authenticated vendor."""
    # Find active verify record
    verify = db.query(VerifyRecord).filter(
        VerifyRecord.vendor_id == current_user.id,
        VerifyRecord.lifecycle_status == LifecycleStatus.ACTIVE
    ).first()
    
    if not verify:
        return []
        
    proofs = db.query(Proof).filter(Proof.verify_id == verify.id).order_by(Proof.created_at.desc()).all()
    
    return [
        {
            "id": str(p.id),
            "filename": p.title or "Document",
            "hash": p.hash_value,
            "blockchain_tx": p.metadata_json.get("tx_hash") if p.metadata_json else None,
            "verify_url": p.metadata_json.get("verify_url") if p.metadata_json else None,
            "created_at": p.created_at.isoformat()
        }
        for p in proofs
    ]


@router.post("/evidence")
async def upload_evidence(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Upload a document, notarize it, and anchor to blockchain."""
    # 1. Get or create VerifyRecord
    verify = db.query(VerifyRecord).filter(
        VerifyRecord.vendor_id == current_user.id,
        VerifyRecord.lifecycle_status == LifecycleStatus.ACTIVE
    ).first()
    
    if not verify:
        verify = VerifyRecord(
            vendor_id=current_user.id,
            verification_level=VerificationLevel.BASIC,
            compliance_score=0
        )
        db.add(verify)
        db.commit()
        db.refresh(verify)
        
    # 2. Read file and hash it
    content = await file.read()
    file_hash = hashlib.sha256(content).hexdigest()
    
    # Check for duplicate hash
    existing = db.query(Proof).filter(Proof.hash_value == file_hash).first()
    if existing:
        raise HTTPException(status_code=409, detail="This document has already been notarized.")
        
    # 3. Anchor to blockchain (Polygon Amoy testnet)
    tx_hash = None
    anchor_status = "pending_anchor"
    try:
        from app.services.blockchain import BlockchainService
        blockchain = BlockchainService()
        tx_hash = await blockchain.anchor_evidence(
            file_hash, metadata=f"vendor_evidence:vendor:{current_user.id}"
        )
        anchor_status = "anchored"
    except Exception as exc:
        logger.warning("Blockchain anchor failed for evidence upload (will retry later): %s", exc)

    explorer_base = settings.POLYGON_EXPLORER_URL.rstrip("/")
    verify_url = (
        f"{explorer_base}/tx/{tx_hash}"
        if tx_hash else None
    )

    # 4. Create Proof record
    proof = Proof(
        verify_id=verify.id,
        hash_value=file_hash,
        title=file.filename,
        metadata_json={
            "size": len(content),
            "content_type": file.content_type,
            "status": anchor_status,
            "tx_hash": tx_hash,
            "verify_url": verify_url,
            "network": "Polygon Amoy Testnet",
            "testnet_notice": "Anchored on Polygon Amoy testnet. Not yet on mainnet.",
        }
    )
    db.add(proof)

    # 5. Update vendor score visibility bonus
    from app.services.scoring import VendorScoreEngine
    db.commit()  # save proof first
    VendorScoreEngine.update_vendor_score(db, str(current_user.id))

    db.refresh(proof)
    return {
        "id": str(proof.id),
        "filename": proof.title,
        "hash": proof.hash_value,
        "created_at": proof.created_at.isoformat(),
        "tx_hash": proof.metadata_json.get("tx_hash"),
        "verify_url": proof.metadata_json.get("verify_url"),
        "network": "Polygon Amoy Testnet",
        "testnet_notice": "Anchored on Polygon Amoy testnet. Not yet on mainnet.",
        "anchor_status": anchor_status,
    }


# ── Profile Management ────────────────────────────────────────────────────────

from pydantic import BaseModel

class ProfileUpdate(BaseModel):
    company: str

@router.get("/profile")
async def get_profile(current_user=Depends(get_current_user)):
    """Return the authenticated vendor's profile."""
    return {
        "id": str(current_user.id),
        "email": current_user.email,
        "company": getattr(current_user, "company", None),
        "role": getattr(current_user, "role", "VENDOR")
    }

@router.patch("/profile")
async def update_profile(
    body: ProfileUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Update vendor profile details."""
    from app.core.models import User
    user = db.query(User).filter(User.id == current_user.id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    user.company = body.company
    db.commit()
    return {"status": "success", "company": user.company}
