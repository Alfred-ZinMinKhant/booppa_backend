"""
SAML 2.0 SP service — Enterprise SSO

Powers the SP side of SAML for tenants on Pro Suite (or any tier with SSO enabled).
Each Organisation gets a unique SP entityID derived from its slug; the IdP
metadata URL configured on `SsoConfig.idp_metadata_url` is fetched at request
time so admins can rotate IdP certs without redeploying.

We use HTTP-POST binding for the ACS and HTTP-Redirect for the AuthnRequest.
AuthnRequests are unsigned (most IdPs accept this for SP-initiated SSO without
mutual TLS). Assertions and responses MUST be signed by the IdP — we reject
unsigned assertions.

pysaml2 is imported lazily so an environment without it still boots; the SAML
endpoints will return 503 with a clear message instead of crashing at import.
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings
from app.core.models_enterprise import Organisation, SsoConfig

logger = logging.getLogger(__name__)


def _base_url() -> str:
    return (getattr(settings, "VERIFY_BASE_URL", None) or "https://app.booppa.io").rstrip("/")


def sp_entity_id(org_slug: str) -> str:
    """Per-tenant SP entityID. Stable across redeploys."""
    return f"{_base_url()}/api/v1/enterprise/sso/saml/metadata/{org_slug}"


def sp_acs_url(org_slug: str) -> str:
    return f"{_base_url()}/api/v1/enterprise/sso/saml/acs/{org_slug}"


def _build_client(sso: SsoConfig, org: Organisation):
    """Construct a pysaml2 client configured against the tenant's IdP."""
    from saml2 import BINDING_HTTP_POST, BINDING_HTTP_REDIRECT
    from saml2.client import Saml2Client
    from saml2.config import Config as Saml2Config

    if not sso.idp_metadata_url:
        raise ValueError("SAML idp_metadata_url is not set for this organisation")

    cfg_dict: dict[str, Any] = {
        "entityid": sp_entity_id(org.slug),
        "metadata": {"remote": [{"url": sso.idp_metadata_url}]},
        "service": {
            "sp": {
                "endpoints": {
                    "assertion_consumer_service": [
                        (sp_acs_url(org.slug), BINDING_HTTP_POST),
                    ],
                },
                "allow_unsolicited": True,
                "authn_requests_signed": False,
                "want_assertions_signed": True,
                "want_response_signed": True,
                "name_id_format": [
                    "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
                    "urn:oasis:names:tc:SAML:2.0:nameid-format:persistent",
                ],
            }
        },
        "allow_unknown_attributes": True,
    }
    sp_config = Saml2Config()
    sp_config.load(cfg_dict)
    return Saml2Client(config=sp_config), BINDING_HTTP_REDIRECT, BINDING_HTTP_POST


def build_login_redirect(sso: SsoConfig, org: Organisation, relay_state: str) -> str:
    """Generate the IdP redirect URL for an SP-initiated AuthnRequest."""
    client, redirect_binding, _ = _build_client(sso, org)
    reqid, info = client.prepare_for_authenticate(
        entityid=sso.idp_entity_id or None,
        relay_state=relay_state,
        binding=redirect_binding,
    )
    # pysaml2 returns headers like [("Location", "https://idp.example/sso?...")]
    for header_name, header_val in info.get("headers", []):
        if header_name.lower() == "location":
            return header_val
    raise RuntimeError("pysaml2 did not return a redirect URL")


def parse_assertion(sso: SsoConfig, org: Organisation, saml_response_b64: str) -> dict[str, Any]:
    """
    Validate a SAML Response and extract subject + attributes.

    Returns:
        {
          "email":      <subject email — required>,
          "name_id":    <raw NameID>,
          "attributes": <dict of attribute → list[str]>,
        }

    Raises ValueError if the assertion is unsigned, expired, or addressed to a
    different audience.
    """
    from saml2 import BINDING_HTTP_POST

    client, _, _ = _build_client(sso, org)
    authn_response = client.parse_authn_request_response(
        saml_response_b64,
        BINDING_HTTP_POST,
    )
    if authn_response is None:
        raise ValueError("SAML response could not be parsed")

    identity = authn_response.get_identity() or {}
    name_id = ""
    try:
        name_id = authn_response.assertion.subject.name_id.text or ""
    except AttributeError:
        pass

    email = ""
    # Prefer an explicit email attribute, fall back to NameID if it's an email.
    for key in ("email", "Email", "mail", "EmailAddress", "emailAddress",
                "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"):
        if key in identity and identity[key]:
            email = identity[key][0]
            break
    if not email and "@" in name_id:
        email = name_id

    if not email:
        raise ValueError("SAML assertion did not carry an email attribute or email-formatted NameID")

    return {
        "email": email.lower().strip(),
        "name_id": name_id,
        "attributes": identity,
    }


def sp_metadata_xml(sso: SsoConfig, org: Organisation) -> str:
    """Generate SP metadata XML so the tenant's IdP admin can register us."""
    from saml2.metadata import entity_descriptor

    client, _, _ = _build_client(sso, org)
    return str(entity_descriptor(client.config))
