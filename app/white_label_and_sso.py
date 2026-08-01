"""
White-label & SSO helpers — V12
Provides branding lookups for PDF generation and OIDC/SAML token exchange stubs.
"""
import logging
from typing import Optional, Dict, Any

from sqlalchemy.orm import Session

from app.core.models import WhiteLabelConfig, SsoConfig

logger = logging.getLogger(__name__)


# ── White-label ────────────────────────────────────────────────────────────────

def get_branding(organisation_id: str, db: Session) -> Optional[Dict[str, Any]]:
    """Return branding dict for an org, or None if not configured."""
    cfg = (
        db.query(WhiteLabelConfig)
        .filter(WhiteLabelConfig.organisation_id == organisation_id)
        .first()
    )
    if not cfg:
        return None
    return {
        "logo_s3_key": cfg.logo_s3_key,
        "primary_color": cfg.primary_color,
        "secondary_color": cfg.secondary_color,
        "footer_text": cfg.footer_text,
        "report_header_text": cfg.report_header_text,
        "custom_domain": cfg.custom_domain,
    }


def resolve_branding_for_owner(
    user_id, db, *, plan_keys, fallback_header: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Return a ``build_doc(branding=...)``-ready dict for a user's own org, or None.

    Single source of truth for "does this customer's PDF carry their brand?", shared
    by the Pro Suite TRM documents and the Buyer Enterprise deliverables — the two
    differ only in the `plan_keys` set they pass, which is what keeps the buyer
    white-label deliberately narrower than Pro Suite's (see BUYER_WHITE_LABEL_PLAN_KEYS
    in app/billing/enforcement.py).

    Returns None when the plan isn't entitled or no config exists, so a lapsed or
    downgraded customer's next cycle silently reverts to Booppa branding.

    Colour mapping is deliberate and matches what pdf_layout.draw_page expects: the
    customer's `primary_color` becomes the header *band* (dict `secondary_color`) and
    their `secondary_color` becomes the accent rule (dict `primary_color`).
    """
    from app.core.models import Organisation, User, WhiteLabelConfig

    org = (
        db.query(Organisation)
        .filter(Organisation.owner_user_id == user_id)
        .order_by(Organisation.created_at.asc())
        .first()
    )
    if not org:
        return None

    owner = db.query(User).filter(User.id == org.owner_user_id).first()
    plan = (getattr(owner, "plan", "") or "").lower().strip()
    if plan not in plan_keys:
        return None

    cfg = (
        db.query(WhiteLabelConfig)
        .filter(WhiteLabelConfig.organisation_id == org.id)
        .first()
    )
    if not cfg:
        return None

    # A logo problem must never break document generation — fall back to Booppa's
    # wordmark, which pdf_layout._resolve_logo already does when logo_bytes is None.
    logo_bytes = None
    if cfg.logo_s3_key:
        try:
            from app.services.storage import S3Service
            _s3 = S3Service()
            logo_bytes = _s3.s3_client.get_object(
                Bucket=_s3.bucket, Key=cfg.logo_s3_key
            )["Body"].read()
        except Exception as wl_err:
            logger.warning("[WhiteLabel] logo fetch failed for org %s: %s", org.id, wl_err)

    return {
        "primary_color": cfg.secondary_color or "#0f172a",
        "secondary_color": cfg.primary_color or "#10b981",
        "footer_text": cfg.footer_text,
        "report_header_text": cfg.report_header_text or fallback_header or org.name,
        "logo_bytes": logo_bytes,
    }


# ── SSO ────────────────────────────────────────────────────────────────────────

def get_sso_config(organisation_id: str, db: Session) -> Optional[SsoConfig]:
    return (
        db.query(SsoConfig)
        .filter(
            SsoConfig.organisation_id == organisation_id,
            SsoConfig.is_active == True,
        )
        .first()
    )


def get_oidc_login_url(sso: SsoConfig, redirect_uri: str, state: str) -> Optional[str]:
    """Build OIDC authorisation URL from stored config."""
    if not sso or sso.protocol != "oidc" or not sso.discovery_url:
        return None
    try:
        import httpx
        meta = httpx.get(sso.discovery_url, timeout=5).json()
        auth_endpoint = meta.get("authorization_endpoint", "")
        params = (
            f"?response_type=code"
            f"&client_id={sso.client_id}"
            f"&redirect_uri={redirect_uri}"
            f"&scope=openid+email+profile"
            f"&state={state}"
        )
        return auth_endpoint + params
    except Exception as e:
        logger.warning("OIDC discovery failed: %s", e)
        return None


async def exchange_oidc_code(sso: SsoConfig, code: str, redirect_uri: str) -> Optional[Dict]:
    """Exchange authorisation code for tokens."""
    if not sso or sso.protocol != "oidc":
        return None
    try:
        import httpx
        meta = httpx.get(sso.discovery_url, timeout=5).json()
        token_endpoint = meta.get("token_endpoint", "")
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": sso.client_id,
                    "client_secret": sso.client_secret,
                },
            )
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.warning("OIDC token exchange failed: %s", e)
    return None


def get_saml_acs_url(organisation_slug: str) -> str:
    """Return the SP ACS URL for SAML responses."""
    from app.core.config import settings
    base = getattr(settings, "VERIFY_BASE_URL", "https://app.booppa.io").rstrip("/")
    return f"{base}/api/v1/enterprise/sso/saml/acs/{organisation_slug}"
