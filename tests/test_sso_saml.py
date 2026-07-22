"""
SSO (SAML 2.0) end-to-end proof.

Pro Suite's SP-side SAML flow (metadata / login-redirect / ACS, all in
app/api/enterprise_api.py + app/services/saml_service.py) previously had zero
test coverage and had never been exercised against a real signed assertion.
This spins up a throwaway, in-process mock IdP (self-signed cert via
`cryptography`, pysaml2's own `saml2.server.Server`) so the test is fully
automated and hermetic — no manual browser step, no dependency on a
third-party IdP being reachable.

Requires the `xmlsec1` system binary (pysaml2 shells out to it to sign/verify
assertions) — see Dockerfile / Dockerfilelocal, and locally `brew install
xmlsec1` / `apt-get install xmlsec1`.
"""
from __future__ import annotations

import base64
import datetime
import http.server
import threading
import uuid

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.core.auth import verify_access_token
from app.core.models import Organisation, OrganisationMember, SsoConfig, User
from tests._test_helpers import make_org, make_user

pytest.importorskip("saml2", reason="pysaml2 not installed")

from saml2 import BINDING_HTTP_REDIRECT  # noqa: E402
from saml2.authn_context import PASSWORD  # noqa: E402
from saml2.config import IdPConfig  # noqa: E402
from saml2.metadata import entity_descriptor  # noqa: E402
from saml2.server import Server  # noqa: E402


def _generate_self_signed_cert(tmp_path):
    """Throwaway RSA keypair + self-signed cert for the mock IdP to sign with."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mock-idp.test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow() - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    key_path = tmp_path / "idp-key.pem"
    cert_path = tmp_path / "idp-cert.pem"
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(key_path), str(cert_path)


class _MetadataHTTPHandler(http.server.BaseHTTPRequestHandler):
    metadata_xml: bytes = b""

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/samlmetadata+xml")
        self.end_headers()
        self.wfile.write(self.metadata_xml)

    def log_message(self, *args):  # silence test output
        pass


@pytest.fixture
def mock_idp(tmp_path):
    """A real (throwaway) SAML IdP: signed metadata served over a local HTTP
    socket (so the SP's `_build_client` remote-metadata fetch is exercised
    unmodified). `server()` builds the `saml2.server.Server` used to mint real
    signed assertions once the SP's own metadata is known (the IdP needs the
    SP registered in its metadata store to respond to it, same as a real IdP
    admin console needs the SP metadata uploaded before first login).
    """
    key_path, cert_path = _generate_self_signed_cert(tmp_path)
    idp_entity_id = "https://mock-idp.test/idp"
    idp_sso_url = "https://mock-idp.test/idp/sso"

    def _build_idp_config(sp_metadata_xml: str | None = None):
        idp_cfg = IdPConfig()
        metadata_cfg = {"inline": [sp_metadata_xml]} if sp_metadata_xml else {"inline": []}
        idp_cfg.load({
            "entityid": idp_entity_id,
            "service": {
                "idp": {
                    "endpoints": {
                        "single_sign_on_service": [(idp_sso_url, BINDING_HTTP_REDIRECT)],
                    },
                    "want_authn_requests_signed": False,
                }
            },
            "key_file": key_path,
            "cert_file": cert_path,
            "metadata": metadata_cfg,
        })
        return idp_cfg

    metadata_xml = str(entity_descriptor(_build_idp_config())).encode("utf-8")
    handler_cls = type("Handler", (_MetadataHTTPHandler,), {"metadata_xml": metadata_xml})
    httpd = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "server": lambda sp_metadata_xml: Server(config=_build_idp_config(sp_metadata_xml)),
            "entity_id": idp_entity_id,
            "metadata_url": f"http://127.0.0.1:{port}/idp-metadata",
        }
    finally:
        httpd.shutdown()
        thread.join(timeout=2)


def _sso_org(db) -> tuple[User, Organisation]:
    owner = make_user(db, plan="pro_suite", company="NovaPay Fintech Pte Ltd")
    org = make_org(db, owner=owner, tier="pro")
    return owner, org


def test_sso_saml_full_round_trip(client, test_db, mock_idp):
    """Metadata -> login redirect -> a REAL signed assertion through the ACS
    endpoint -> minted session -> JIT-provisioned user. Exercises the exact
    code path a browser-driven login would hit, just without the browser.
    """
    owner, org = _sso_org(test_db)
    sso = SsoConfig(
        id=uuid.uuid4(),
        organisation_id=org.id,
        protocol="saml",
        idp_metadata_url=mock_idp["metadata_url"],
        idp_entity_id=mock_idp["entity_id"],
        is_active=True,
    )
    test_db.add(sso)
    test_db.commit()

    # 1. SP metadata renders (what we'd hand a real IdP admin to register us).
    meta_resp = client.get(f"/api/v1/enterprise/sso/saml/metadata/{org.slug}")
    assert meta_resp.status_code == 200
    assert b"EntityDescriptor" in meta_resp.content

    # 2. SP-initiated login redirects to the configured IdP's SSO endpoint.
    login_resp = client.get(
        f"/api/v1/enterprise/sso/saml/login/{org.slug}", follow_redirects=False,
    )
    assert login_resp.status_code == 302
    assert mock_idp["entity_id"].split("//")[1].split("/")[0] in login_resp.headers["location"] \
        or "mock-idp.test" in login_resp.headers["location"]

    # 3. The mock IdP mints a REAL signed assertion for this SP + a test user
    #    (IdP-initiated shape — allow_unsolicited=True on the SP side permits this,
    #    same as many real IdP admin consoles default to for SP-initiated setups
    #    that skip the AuthnRequest round-trip in automated checks). The IdP needs
    #    the SP's own metadata registered first — same as a real IdP admin console.
    from app.services.saml_service import sp_acs_url, sp_entity_id

    idp_server = mock_idp["server"](meta_resp.text)
    identity = {"email": ["new.employee@novapay.example"]}
    authn_response = idp_server.create_authn_response(
        identity=identity,
        in_response_to="_test_unsolicited",
        destination=sp_acs_url(org.slug),
        sp_entity_id=sp_entity_id(org.slug),
        userid="new.employee@novapay.example",
        name_id=None,
        authn=dict(class_ref=PASSWORD, authn_auth=mock_idp["entity_id"]),
        sign_response=True,
        sign_assertion=True,
    )
    saml_response_b64 = base64.b64encode(str(authn_response).encode("utf-8")).decode("ascii")

    acs_resp = client.post(
        f"/api/v1/enterprise/sso/saml/acs/{org.slug}",
        data={"SAMLResponse": saml_response_b64, "RelayState": "/vendor/trm"},
        follow_redirects=False,
    )
    assert acs_resp.status_code == 302, acs_resp.text
    location = acs_resp.headers["location"]
    assert "access_token=" in location
    assert "refresh_token=" in location

    # 4. The minted token is a real, verifiable Booppa access token.
    fragment = location.split("#", 1)[1]
    params = dict(p.split("=", 1) for p in fragment.split("&"))
    payload = verify_access_token(params["access_token"])
    assert payload["sub"] == "new.employee@novapay.example"

    # 5. JIT-provisioning actually happened: real User + OrganisationMember rows.
    new_user = test_db.query(User).filter(User.email == "new.employee@novapay.example").first()
    assert new_user is not None
    membership = (
        test_db.query(OrganisationMember)
        .filter(
            OrganisationMember.organisation_id == org.id,
            OrganisationMember.user_id == new_user.id,
        )
        .first()
    )
    assert membership is not None
    assert membership.role == "member"


def test_sso_saml_inactive_config_rejected(client, test_db, mock_idp):
    """A lapsed/never-activated SsoConfig must not let anyone in — belt and
    suspenders alongside the Pro Suite plan gate in `_resolve_saml_context`.
    """
    owner, org = _sso_org(test_db)
    sso = SsoConfig(
        id=uuid.uuid4(),
        organisation_id=org.id,
        protocol="saml",
        idp_metadata_url=mock_idp["metadata_url"],
        idp_entity_id=mock_idp["entity_id"],
        is_active=False,
    )
    test_db.add(sso)
    test_db.commit()

    resp = client.get(f"/api/v1/enterprise/sso/saml/login/{org.slug}", follow_redirects=False)
    assert resp.status_code == 400
