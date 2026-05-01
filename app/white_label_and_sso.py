"""
White-label & SSO helpers — V12
Provides branding lookups for PDF generation and OIDC/SAML token exchange stubs.
"""
import logging
from typing import Optional, Dict, Any

from sqlalchemy.orm import Session

from app.core.models_enterprise import WhiteLabelConfig, SsoConfig

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
