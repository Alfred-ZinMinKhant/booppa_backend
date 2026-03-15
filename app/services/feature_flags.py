"""
Feature Flag Service
====================
Redis-backed feature flags with auto-activation based on growth metrics.
Phases 1-4 unlock modules automatically when thresholds are met.

Phase 1 (Always on): Core auth, vendor, transact, verify, badge, marketplace
Phase 2 (>500 vendors, >100 RFPs, >10 Verify Proofs): Comparison, SEO
Phase 3 (>1500 vendors, >300 RFPs, >50 certs): Ranking, Graph
Phase 4 (>3000 vendors, >1000 RFPs): Competition, Insight Dome, Procurement Automation
"""

import logging
import json
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session
from app.core.config import settings
from app.core.db import SessionLocal

logger = logging.getLogger(__name__)

# ── Flag Definitions ───────────────────────────────────────────────────────────

FEATURE_FLAGS = {
    # Phase 2
    "FEATURE_COMPARISON": {
        "phase": 2,
        "description": "Vendor comparison engine",
        "thresholds": {"min_vendors": 500, "min_rfps": 100, "min_verify_proofs": 10},
    },
    "FEATURE_SEO": {
        "phase": 2,
        "description": "Programmatic SEO pages",
        "thresholds": {"min_vendors": 500, "min_rfps": 100, "min_verify_proofs": 10},
    },
    # Phase 3
    "FEATURE_RANKING": {
        "phase": 3,
        "description": "Percentile ranking system",
        "thresholds": {"min_vendors": 1500, "min_rfps": 300, "min_certificates": 50},
    },
    "FEATURE_GRAPH": {
        "phase": 3,
        "description": "Vendor relationship graph",
        "thresholds": {"min_vendors": 1500, "min_rfps": 300, "min_certificates": 50},
    },
    # Phase 4
    "FEATURE_COMPETITION": {
        "phase": 4,
        "description": "Competitive intelligence",
        "thresholds": {"min_vendors": 3000, "min_rfps": 1000},
    },
    "FEATURE_INSIGHT": {
        "phase": 4,
        "description": "Insight Dome intelligence hub",
        "thresholds": {"min_vendors": 3000, "min_rfps": 1000},
    },
    "FEATURE_PROCUREMENT_AUTOMATION": {
        "phase": 4,
        "description": "Automated procurement workflows",
        "thresholds": {"min_vendors": 3000, "min_rfps": 1000},
    },
}

# ── Redis-backed Flag Store ────────────────────────────────────────────────────

_redis_client = None


def _get_redis():
    """Lazy Redis connection."""
    global _redis_client
    if _redis_client is None:
        try:
            import redis as redis_lib
            _redis_client = redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
            _redis_client.ping()
        except Exception as e:
            logger.warning(f"Redis unavailable for feature flags: {e}")
            _redis_client = None
    return _redis_client


def is_feature_enabled(flag_name: str) -> bool:
    """Check if a feature flag is enabled. Falls back to env vars then DB."""
    # 1. Check environment variable override
    import os
    env_val = os.environ.get(flag_name)
    if env_val is not None:
        return env_val.lower() in ("true", "1", "yes")

    # 2. Check Redis
    r = _get_redis()
    if r:
        try:
            val = r.get(f"feature_flag:{flag_name}")
            if val is not None:
                return val.lower() in ("true", "1", "yes")
        except Exception:
            pass

    # 3. Check DB as last resort
    try:
        from app.core.models_v10 import FeatureFlag
        db = SessionLocal()
        try:
            flag = db.query(FeatureFlag).filter(FeatureFlag.flag_name == flag_name).first()
            if flag:
                return flag.enabled
        finally:
            db.close()
    except Exception:
        pass

    return False


def set_feature_flag(flag_name: str, enabled: bool, enabled_by: str = "manual"):
    """Set a feature flag in Redis and DB."""
    r = _get_redis()
    if r:
        try:
            r.set(f"feature_flag:{flag_name}", "true" if enabled else "false")
        except Exception as e:
            logger.warning(f"Failed to set flag in Redis: {e}")

    # Persist to DB
    try:
        from app.core.models_v10 import FeatureFlag
        db = SessionLocal()
        try:
            flag = db.query(FeatureFlag).filter(FeatureFlag.flag_name == flag_name).first()
            if flag:
                flag.enabled = enabled
                flag.enabled_by = enabled_by
                flag.enabled_at = datetime.utcnow() if enabled else None
            else:
                definition = FEATURE_FLAGS.get(flag_name, {})
                flag = FeatureFlag(
                    flag_name=flag_name,
                    enabled=enabled,
                    description=definition.get("description", ""),
                    phase=definition.get("phase", 1),
                    activation_thresholds=definition.get("thresholds"),
                    enabled_by=enabled_by,
                    enabled_at=datetime.utcnow() if enabled else None,
                )
                db.add(flag)
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"Failed to persist flag to DB: {e}")


def get_all_flags() -> dict:
    """Get all feature flags and their status."""
    flags = {}
    for flag_name, definition in FEATURE_FLAGS.items():
        flags[flag_name] = {
            "enabled": is_feature_enabled(flag_name),
            "phase": definition["phase"],
            "description": definition["description"],
            "thresholds": definition.get("thresholds", {}),
        }
    return flags


def get_growth_metrics(db: Session) -> dict:
    """Gather current growth metrics for auto-activation checks."""
    from app.core.models import User
    from app.core.models_v6 import VerifyRecord
    from app.core.models_v8 import EvidencePackage, RfpRequirement
    from app.core.models_v10 import MarketplaceVendor, CertificateLog

    vendor_count = db.query(User).filter(User.role == "vendor").count()
    marketplace_count = db.query(MarketplaceVendor).count()
    total_vendors = vendor_count + marketplace_count

    rfp_count = db.query(RfpRequirement).count()
    verify_count = db.query(VerifyRecord).count()
    cert_count = db.query(CertificateLog).count()
    evidence_count = db.query(EvidencePackage).filter(EvidencePackage.status == "READY").count()

    return {
        "total_vendors": total_vendors,
        "registered_vendors": vendor_count,
        "marketplace_vendors": marketplace_count,
        "rfp_count": rfp_count,
        "verify_proofs": verify_count,
        "certificates": cert_count,
        "evidence_packages": evidence_count,
    }


def check_auto_activation(db: Session) -> list[str]:
    """Check if any feature flags should be auto-activated based on metrics."""
    metrics = get_growth_metrics(db)
    newly_activated = []

    for flag_name, definition in FEATURE_FLAGS.items():
        if is_feature_enabled(flag_name):
            continue

        thresholds = definition.get("thresholds", {})
        should_activate = True

        if "min_vendors" in thresholds and metrics["total_vendors"] < thresholds["min_vendors"]:
            should_activate = False
        if "min_rfps" in thresholds and metrics["rfp_count"] < thresholds["min_rfps"]:
            should_activate = False
        if "min_verify_proofs" in thresholds and metrics["verify_proofs"] < thresholds["min_verify_proofs"]:
            should_activate = False
        if "min_certificates" in thresholds and metrics["certificates"] < thresholds["min_certificates"]:
            should_activate = False

        if should_activate:
            set_feature_flag(flag_name, True, enabled_by="auto")
            newly_activated.append(flag_name)
            logger.info(f"Auto-activated feature flag: {flag_name}")

    return newly_activated


def require_feature(flag_name: str):
    """FastAPI dependency that returns 404 if feature is not enabled."""
    from fastapi import HTTPException

    def _check():
        if not is_feature_enabled(flag_name):
            definition = FEATURE_FLAGS.get(flag_name, {})
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "feature_not_available",
                    "feature": flag_name,
                    "phase": definition.get("phase", "unknown"),
                    "description": definition.get("description", ""),
                    "message": f"This feature requires Phase {definition.get('phase', '?')} activation.",
                },
            )
    return _check
