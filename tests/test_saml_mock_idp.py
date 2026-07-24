"""The mock IdP must produce assertions our SP genuinely validates.

The positive case alone proves little — an SP that accepted anything would pass
it. The tampered case is what shows the signature is actually checked, so both
are asserted together.

Skipped when `xmlsec1` is absent: pysaml2 shells out to it for signing, so its
absence means "cannot run the demonstration", not "SSO is broken".
"""
import pytest

from app.core.models import Organisation, SsoConfig
from app.services import saml_service
from app.services.saml_mock_idp import MOCK_IDP_ENTITY_ID, MockIdp, xmlsec1_available

pytestmark = pytest.mark.skipif(
    not xmlsec1_available(), reason="xmlsec1 not installed — cannot sign test assertions"
)

DEMO_EMAIL = "sso.demo@novapay.test"


@pytest.fixture
def idp():
    inst = MockIdp()
    yield inst
    inst.cleanup()


@pytest.fixture
def sp(idp):
    """An unsaved org + SSO config pointing at the mock IdP.

    Neither needs persisting — `saml_service` only reads attributes off them.
    """
    org = Organisation(name="NovaPay Demo", slug="novapay-demo")
    sso = SsoConfig(
        protocol="saml",
        idp_metadata_url=idp.metadata_url,
        idp_entity_id=MOCK_IDP_ENTITY_ID,
        is_active=True,
    )
    return org, sso


def _assertion(idp, org, *, email=DEMO_EMAIL, tamper=False):
    return idp.build_signed_response(
        acs_url=saml_service.sp_acs_url(org.slug),
        sp_entity_id=saml_service.sp_entity_id(org.slug),
        email=email,
        tamper=tamper,
    )


def test_metadata_publishes_the_signing_certificate(idp):
    """Without a cert in the metadata the SP has nothing to verify against."""
    xml = idp.metadata_path.read_text()
    assert "<ds:X509Certificate>" in xml
    assert idp.cert_b64 in xml
    assert idp.metadata_url.startswith("file://")


def test_signed_assertion_is_accepted_and_yields_the_subject(idp, sp):
    org, sso = sp
    identity = saml_service.parse_assertion(sso, org, _assertion(idp, org))

    assert identity["email"] == DEMO_EMAIL
    assert identity["name_id"] == DEMO_EMAIL
    assert identity["attributes"]["displayName"] == ["Demo SSO User"]


def test_tampered_assertion_is_rejected(idp, sp):
    """The negative control. If this ever passes, the signature check is dead."""
    org, sso = sp
    with pytest.raises(Exception) as exc:
        saml_service.parse_assertion(sso, org, _assertion(idp, org, tamper=True))
    assert "signature" in str(exc.value).lower()


def test_assertion_for_another_sp_is_rejected(idp, sp):
    """Audience restriction: an assertion minted for a different tenant's SP
    entityID must not authenticate against this one."""
    org, sso = sp
    other = idp.build_signed_response(
        acs_url=saml_service.sp_acs_url("someone-else"),
        sp_entity_id=saml_service.sp_entity_id("someone-else"),
        email=DEMO_EMAIL,
    )
    with pytest.raises(Exception):
        saml_service.parse_assertion(sso, org, other)
