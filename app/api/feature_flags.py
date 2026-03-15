"""
Feature Flag API Routes
=======================
Admin endpoints for managing feature flags and checking auto-activation.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.core.db import get_db
from app.services.feature_flags import (
    get_all_flags, is_feature_enabled, set_feature_flag,
    check_auto_activation, get_growth_metrics,
)

router = APIRouter()


class SetFlagRequest(BaseModel):
    flag_name: str
    enabled: bool


@router.get("/flags")
async def list_flags():
    """List all feature flags and their status."""
    return get_all_flags()


@router.get("/flags/{flag_name}")
async def get_flag(flag_name: str):
    """Check if a specific feature flag is enabled."""
    return {
        "flag_name": flag_name,
        "enabled": is_feature_enabled(flag_name),
    }


@router.post("/flags")
async def update_flag(request: SetFlagRequest):
    """Set a feature flag (admin only)."""
    set_feature_flag(request.flag_name, request.enabled, enabled_by="admin")
    return {
        "flag_name": request.flag_name,
        "enabled": request.enabled,
        "message": f"Flag {'enabled' if request.enabled else 'disabled'}",
    }


@router.post("/auto-activate")
async def trigger_auto_activation(db: Session = Depends(get_db)):
    """Trigger auto-activation check based on current growth metrics."""
    activated = check_auto_activation(db)
    metrics = get_growth_metrics(db)
    return {
        "newly_activated": activated,
        "current_metrics": metrics,
    }


@router.get("/metrics")
async def get_metrics(db: Session = Depends(get_db)):
    """Get current growth metrics."""
    return get_growth_metrics(db)
