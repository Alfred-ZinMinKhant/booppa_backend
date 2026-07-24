"""Self-hosted mock SAML 2.0 IdP — **test/demo only**.

Why this exists: "the SSO configuration page renders" is not evidence that SSO
works. This module mints a throwaway RSA keypair, publishes IdP metadata
carrying its certificate, and produces a *signed* SAML Response addressed to the
tenant's real ACS URL. The harness then POSTs it at the real
`POST /enterprise/sso/saml/acs/{org_slug}` route, so `saml_service.parse_assertion`
— signature validation, audience/destination checks, attribute extraction — and
the JIT-provisioning + token-minting that follows are genuinely exercised.

Two hard boundaries:

* **Never reachable in production.** Nothing here is mounted on a router. The only
  callers are the Pro Suite demo harness and its tests, both of which write the
  generated metadata to a temp file and point `SsoConfig.idp_metadata_url` at it
  via the `file://` form. A real tenant's config always points at their own IdP.
* **Signing is real.** The response is signed with `xmlsec1` through pysaml2's own
  signer, not stubbed — a mock IdP that skips signing would make the round trip
  prove nothing, because the SP's signature check is the whole point.

Requires the `xmlsec1` binary on PATH (pysaml2 shells out to it). Callers should
treat its absence as "cannot demonstrate", not as a failure of SSO itself —
`xmlsec1_available()` is provided for that check.
"""
from __future__ import annotations

import base64
import logging
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

MOCK_IDP_ENTITY_ID = "https://mock-idp.booppa.test/metadata"
MOCK_IDP_SSO_URL = "https://mock-idp.booppa.test/sso"


def xmlsec1_available() -> bool:
    """True when pysaml2 can sign — it shells out to the `xmlsec1` binary."""
    return shutil.which("xmlsec1") is not None


def _lax_key_search_flag() -> list[str]:
    """`--lax-key-search` on xmlsec1 >= 1.3, empty list below that.

    1.3 tightened key selection: a signing key that isn't matched by the
    template's <KeyInfo> is refused with KEY-NOT-FOUND. Our template leaves
    <X509Certificate/> empty for xmlsec1 to populate, so nothing matches yet and
    the flag is required. Older builds don't recognise the flag at all, hence
    the version probe rather than passing it unconditionally.
    """
    try:
        out = subprocess.run(
            ["xmlsec1", "--version"], capture_output=True, text=True, timeout=10
        ).stdout
        ver = out.strip().split()[1]
        major, minor = (int(p) for p in ver.split(".")[:2])
        if (major, minor) >= (1, 3):
            return ["--lax-key-search"]
    except Exception as exc:
        logger.debug("[MockIdp] xmlsec1 version probe failed (%s); omitting flag", exc)
    return []


class MockIdp:
    """A throwaway SAML IdP: one keypair, one metadata document, signed responses.

    Instances own a temp directory holding the key, cert and metadata XML. Keep
    the instance alive for as long as the `SsoConfig` points at its metadata
    file; `cleanup()` removes the directory.
    """

    def __init__(self, *, workdir: str | None = None):
        self.dir = Path(workdir or tempfile.mkdtemp(prefix="booppa-mock-idp-"))
        self.key_path = self.dir / "idp.key"
        self.cert_path = self.dir / "idp.crt"
        self.metadata_path = self.dir / "idp-metadata.xml"
        self._generate_keypair()
        self.metadata_path.write_text(self._metadata_xml(), encoding="utf-8")

    # ── setup ────────────────────────────────────────────────────────────────
    def _generate_keypair(self) -> None:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "Booppa Mock IdP (demo only)"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Booppa Demo Harness"),
        ])
        now = datetime.utcnow()
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=1))
            .sign(key, hashes.SHA256())
        )
        self.key_path.write_bytes(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        self.cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    @property
    def cert_b64(self) -> str:
        """Bare base64 DER of the cert, as SAML metadata's X509Certificate wants."""
        body = self.cert_path.read_text(encoding="utf-8").strip().splitlines()
        return "".join(l for l in body if "-----" not in l)

    @property
    def metadata_url(self) -> str:
        """`file://` form consumed by `saml_service._build_client`."""
        return f"file://{self.metadata_path}"

    def _metadata_xml(self) -> str:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"
                     xmlns:ds="http://www.w3.org/2000/09/xmldsig#"
                     entityID="{MOCK_IDP_ENTITY_ID}">
  <md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol"
                       WantAuthnRequestsSigned="false">
    <md:KeyDescriptor use="signing">
      <ds:KeyInfo><ds:X509Data><ds:X509Certificate>{self.cert_b64}</ds:X509Certificate></ds:X509Data></ds:KeyInfo>
    </md:KeyDescriptor>
    <md:NameIDFormat>urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress</md:NameIDFormat>
    <md:SingleSignOnService
        Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
        Location="{MOCK_IDP_SSO_URL}"/>
  </md:IDPSSODescriptor>
</md:EntityDescriptor>
"""

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    # ── assertion minting ────────────────────────────────────────────────────
    def build_signed_response(
        self,
        *,
        acs_url: str,
        sp_entity_id: str,
        email: str,
        full_name: str = "Demo SSO User",
        tamper: bool = False,
    ) -> str:
        """Return a base64 SAML Response, signed, ready to POST as `SAMLResponse`.

        `tamper=True` alters the NameID *after* signing — the negative control.
        A round trip that only ever shows a valid assertion being accepted does
        not demonstrate the signature is actually checked; the tampered one
        must be rejected for the positive result to mean anything.
        """
        signed = self._build_signed_response_xml(
            acs_url=acs_url, sp_entity_id=sp_entity_id, email=email, full_name=full_name
        )
        if tamper:
            # Rewrite the subject inside the *already signed* assertion. Every
            # byte of that element is covered by the assertion digest, so a
            # correct SP must reject this.
            signed = signed.replace(email, "attacker@evil.test")
        return base64.b64encode(signed.encode("utf-8")).decode("ascii")

    def _build_signed_response_xml(
        self, *, acs_url: str, sp_entity_id: str, email: str, full_name: str
    ) -> str:
        """Sign the assertion, embed it, then sign the enclosing Response.

        Two passes, in this order, because our SP config sets both
        `want_assertions_signed` and `want_response_signed` (saml_service.py) and
        the Response digest must cover the assertion's finished signature. Each
        `xmlsec1 --sign` run signs the first template it encounters, so the
        assertion is signed as a standalone document first and pasted in.
        """
        now = datetime.utcnow()
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        issue_instant = now.strftime(fmt)
        not_on_or_after = (now + timedelta(minutes=5)).strftime(fmt)
        not_before = (now - timedelta(minutes=5)).strftime(fmt)
        resp_id = "_" + uuid.uuid4().hex
        assertion_id = "_" + uuid.uuid4().hex

        signed_assertion = self._sign(
            self._assertion_xml(
                assertion_id=assertion_id, acs_url=acs_url, sp_entity_id=sp_entity_id,
                email=email, full_name=full_name, issue_instant=issue_instant,
                not_before=not_before, not_on_or_after=not_on_or_after,
            ),
            id_attr="urn:oasis:names:tc:SAML:2.0:assertion:Assertion",
        )
        # Drop the XML declaration — it can't appear mid-document.
        if signed_assertion.lstrip().startswith("<?xml"):
            signed_assertion = signed_assertion.split("?>", 1)[1].lstrip()

        response = f"""<?xml version="1.0" encoding="UTF-8"?>
<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"
                xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"
                ID="{resp_id}" Version="2.0"
                IssueInstant="{issue_instant}" Destination="{acs_url}">
  <saml:Issuer>{MOCK_IDP_ENTITY_ID}</saml:Issuer>
  {self._signature_template(resp_id)}
  <samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>
  {signed_assertion}
</samlp:Response>
"""
        return self._sign(
            response, id_attr="urn:oasis:names:tc:SAML:2.0:protocol:Response"
        )

    def _signature_template(self, ref_id: str) -> str:
        """Enveloped-signature template. `<X509Certificate/>` is left empty for
        xmlsec1 to populate from the signing key's cert."""
        return f"""<ds:Signature xmlns:ds="http://www.w3.org/2000/09/xmldsig#">
      <ds:SignedInfo>
        <ds:CanonicalizationMethod Algorithm="http://www.w3.org/2001/10/xml-exc-c14n#"/>
        <ds:SignatureMethod Algorithm="http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"/>
        <ds:Reference URI="#{ref_id}">
          <ds:Transforms>
            <ds:Transform Algorithm="http://www.w3.org/2000/09/xmldsig#enveloped-signature"/>
            <ds:Transform Algorithm="http://www.w3.org/2001/10/xml-exc-c14n#"/>
          </ds:Transforms>
          <ds:DigestMethod Algorithm="http://www.w3.org/2001/04/xmlenc#sha256"/>
          <ds:DigestValue></ds:DigestValue>
        </ds:Reference>
      </ds:SignedInfo>
      <ds:SignatureValue></ds:SignatureValue>
      <ds:KeyInfo><ds:X509Data><ds:X509Certificate/></ds:X509Data></ds:KeyInfo>
    </ds:Signature>"""

    def _assertion_xml(
        self, *, assertion_id: str, acs_url: str, sp_entity_id: str, email: str,
        full_name: str, issue_instant: str, not_before: str, not_on_or_after: str,
    ) -> str:
        # The <ds:Signature> must sit immediately after <Issuer> per the SAML schema.
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"
                ID="{assertion_id}" Version="2.0" IssueInstant="{issue_instant}">
    <saml:Issuer>{MOCK_IDP_ENTITY_ID}</saml:Issuer>
    {self._signature_template(assertion_id)}
    <saml:Subject>
      <saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">{email}</saml:NameID>
      <saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">
        <saml:SubjectConfirmationData NotOnOrAfter="{not_on_or_after}" Recipient="{acs_url}"/>
      </saml:SubjectConfirmation>
    </saml:Subject>
    <saml:Conditions NotBefore="{not_before}" NotOnOrAfter="{not_on_or_after}">
      <saml:AudienceRestriction><saml:Audience>{sp_entity_id}</saml:Audience></saml:AudienceRestriction>
    </saml:Conditions>
    <saml:AuthnStatement AuthnInstant="{issue_instant}" SessionIndex="{assertion_id}">
      <saml:AuthnContext>
        <saml:AuthnContextClassRef>urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport</saml:AuthnContextClassRef>
      </saml:AuthnContext>
    </saml:AuthnStatement>
    <saml:AttributeStatement>
      <saml:Attribute Name="email" NameFormat="urn:oasis:names:tc:SAML:2.0:attrname-format:basic">
        <saml:AttributeValue>{email}</saml:AttributeValue>
      </saml:Attribute>
      <saml:Attribute Name="displayName" NameFormat="urn:oasis:names:tc:SAML:2.0:attrname-format:basic">
        <saml:AttributeValue>{full_name}</saml:AttributeValue>
      </saml:Attribute>
    </saml:AttributeStatement>
</saml:Assertion>
"""

    def _sign(self, xml: str, *, id_attr: str) -> str:
        """Enveloped-sign `xml` with xmlsec1. Raises if it isn't available.

        `id_attr` names the element whose `ID` attribute the template's
        `<Reference URI="#...">` points at — xmlsec1 will not resolve a
        same-document reference unless told which attribute is the ID.
        """
        if not xmlsec1_available():
            raise RuntimeError(
                "xmlsec1 is not on PATH — cannot sign the mock assertion. "
                "Install xmlsec1 (brew install libxmlsec1) to run the SSO round trip."
            )
        src = self.dir / f"resp-{uuid.uuid4().hex}.xml"
        src.write_text(xml, encoding="utf-8")
        try:
            proc = subprocess.run(
                [
                    "xmlsec1", "--sign",
                    *_lax_key_search_flag(),
                    "--privkey-pem", f"{self.key_path},{self.cert_path}",
                    "--id-attr:ID", id_attr,
                    str(src),
                ],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"xmlsec1 signing failed: {proc.stderr.strip()}")
            return proc.stdout
        finally:
            src.unlink(missing_ok=True)
