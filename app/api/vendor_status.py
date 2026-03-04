"""
Vendor Status Routes — V8
==========================
GET /api/vendor/status           → full VendorStatusProfile (auth'd vendor)
GET /api/vendor/sector-pressure  → sector competitive pressure snapshot + message
GET /api/vendor/dashboard-cal    → CAL payload: ladder + suggestion + message + sectorPressure
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.auth import get_current_user
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
