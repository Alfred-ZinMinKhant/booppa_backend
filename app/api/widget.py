"""
Widget API Routes
=================
Embeddable verification badge widget for external sites.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session
from app.core.db import get_db
from app.core.models import User
from app.core.models_v6 import VerifyRecord, VendorScore
from app.core.models_v8 import VendorStatusSnapshot

router = APIRouter()


@router.get("/badge/{vendor_id}.svg")
async def badge_svg(vendor_id: str, db: Session = Depends(get_db)):
    """Get SVG badge for embedding on vendor's website."""
    user = db.query(User).filter(User.id == vendor_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Vendor not found")

    status = db.query(VendorStatusSnapshot).filter(
        VendorStatusSnapshot.vendor_id == vendor_id
    ).first()

    depth = status.verification_depth if status else "UNVERIFIED"
    color = {
        "CERTIFIED": "#10b981",
        "DEEP": "#10b981",
        "STANDARD": "#3b82f6",
        "BASIC": "#f59e0b",
        "UNVERIFIED": "#94a3b8",
    }.get(depth, "#94a3b8")

    label = f"Booppa Verified: {depth}"
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="200" height="28" viewBox="0 0 200 28">
  <rect width="200" height="28" rx="4" fill="{color}"/>
  <text x="100" y="18" text-anchor="middle" fill="white" font-family="sans-serif" font-size="11" font-weight="600">{label}</text>
</svg>'''

    return HTMLResponse(content=svg, media_type="image/svg+xml", headers={
        "Cache-Control": "public, max-age=3600",
    })


@router.get("/badge/{vendor_id}")
async def badge_json(vendor_id: str, db: Session = Depends(get_db)):
    """Get badge data as JSON for custom rendering."""
    user = db.query(User).filter(User.id == vendor_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Vendor not found")

    status = db.query(VendorStatusSnapshot).filter(
        VendorStatusSnapshot.vendor_id == vendor_id
    ).first()

    score = db.query(VendorScore).filter(VendorScore.vendor_id == vendor_id).first()

    return {
        "vendor_id": str(user.id),
        "company": user.company or user.full_name,
        "verification_depth": status.verification_depth if status else "UNVERIFIED",
        "procurement_readiness": status.procurement_readiness if status else "NOT_READY",
        "confidence_score": status.confidence_score if status else 0,
        "total_score": score.total_score if score else 0,
        "badge_url": f"/api/v1/widget/badge/{vendor_id}.svg",
    }


@router.get("/embed/{vendor_id}")
async def embed_widget(vendor_id: str, db: Session = Depends(get_db)):
    """Get HTML embed code for vendor badge."""
    user = db.query(User).filter(User.id == vendor_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Vendor not found")

    from app.core.config import settings
    base_url = settings.VERIFY_BASE_URL.rstrip("/")

    embed_html = f'''<!-- Booppa Verification Badge -->
<a href="{base_url}/verify/{vendor_id}" target="_blank" rel="noopener">
  <img src="{base_url}/api/v1/widget/badge/{vendor_id}.svg" alt="Booppa Verified" width="200" height="28" />
</a>'''

    return {
        "vendor_id": str(user.id),
        "embed_html": embed_html,
        "badge_svg_url": f"{base_url}/api/v1/widget/badge/{vendor_id}.svg",
        "badge_json_url": f"{base_url}/api/v1/widget/badge/{vendor_id}",
        "verify_url": f"{base_url}/verify/{vendor_id}",
    }
