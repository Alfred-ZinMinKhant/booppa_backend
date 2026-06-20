"""Vendor Proof §7 fixes: real (non-hardcoded) score, ACRA UEN lookup, and a
generated + anchored certificate PDF.

Covers app/api/stripe_webhook.py:_fulfill_vendor_proof and
app/services/vendor_proof_generator.py.
"""
import asyncio


def _make_user(db, email, uen=None):
    from app.core.models import User
    u = User(email=email, hashed_password="x", role="VENDOR",
             company="Crayon Singapore", website="https://crayon.com/", uen=uen)
    db.add(u); db.commit(); db.refresh(u)
    return u


def _vp_report(db, owner_id, uen=None):
    from app.core.models import Report
    ad = {"contact_email": None}
    if uen:
        ad["uen"] = uen
    r = Report(owner_id=owner_id, framework="vendor_proof",
               company_name="Crayon Singapore", status="pending", assessment_data=ad)
    db.add(r); db.commit(); db.refresh(r)
    return r


def _run(report_id, email, mocker, anchor="0xanchored"):
    async def fake_email(self, *a, **k):
        return True

    async def fake_upload(self, pdf_bytes, report_id):
        assert pdf_bytes[:4] == b"%PDF"
        return f"https://s3.example/{report_id}.pdf"

    async def fake_anchor(self, evidence_hash, metadata="", force=False):
        return anchor

    mocker.patch("app.services.email_service.EmailService.send_html_email", fake_email)
    mocker.patch("app.services.scoring.VendorScoreEngine.update_vendor_score", return_value=None)
    mocker.patch("app.services.storage.S3Service.upload_pdf", fake_upload)
    mocker.patch("app.services.blockchain.BlockchainService.anchor_evidence", fake_anchor)
    from app.api.stripe_webhook import _fulfill_vendor_proof
    asyncio.run(_fulfill_vendor_proof(str(report_id), email))


def test_score_uses_real_pdpa_not_hardcoded_30(test_db, mocker):
    """A vendor with a strong PDPA scan must get that score on VerifyRecord and
    VendorScore — not the legacy hardcoded 30."""
    from app.core.models import Report
    from app.core.models_v6 import VerifyRecord, VendorScore

    email = "vpcert+strong@booppa.io"
    user = _make_user(test_db, email)
    test_db.add(Report(owner_id=user.id, framework="pdpa_quick_scan",
                       company_name="Crayon Singapore", status="completed",
                       assessment_data={"compliance_score": 82}))
    test_db.commit()
    rpt = _vp_report(test_db, user.id)

    _run(rpt.id, email, mocker)

    test_db.expire_all()
    verify = test_db.query(VerifyRecord).filter(VerifyRecord.vendor_id == user.id).first()
    score = test_db.query(VendorScore).filter(VendorScore.vendor_id == user.id).first()
    assert verify is not None and verify.compliance_score == 82
    assert score is not None and score.compliance_score == 82


def test_no_scan_keeps_identity_floor_30(test_db, mocker):
    from app.core.models_v6 import VerifyRecord
    email = "vpcert+noscan@booppa.io"
    user = _make_user(test_db, email)
    rpt = _vp_report(test_db, user.id)

    _run(rpt.id, email, mocker)

    test_db.expire_all()
    verify = test_db.query(VerifyRecord).filter(VerifyRecord.vendor_id == user.id).first()
    assert verify is not None and verify.compliance_score == 30


def test_acra_lookup_populates_assessment_data(test_db, mocker):
    """When the UEN matches an imported ACRA row, registration details are
    persisted on the report and acra_verified flips true."""
    from app.core.models import Report
    from app.core.models_v10 import DiscoveredVendor

    uen = "201912345A"
    test_db.add(DiscoveredVendor(
        company_name="Crayon Singapore Pte Ltd", uen=uen, entity_type="PRIVATE COMPANY",
        registration_date="2019-01-05", industry="IT", source="acra",
    ))
    test_db.commit()

    email = "vpcert+acra@booppa.io"
    user = _make_user(test_db, email, uen=uen)
    rpt = _vp_report(test_db, user.id, uen=uen)

    _run(rpt.id, email, mocker)

    test_db.expire_all()
    r = test_db.query(Report).filter(Report.id == rpt.id).first()
    assert r.assessment_data.get("acra_verified") is True
    assert r.assessment_data.get("acra_entity_type") == "PRIVATE COMPANY"
    assert r.assessment_data.get("acra_registration_date") == "2019-01-05"


def test_certificate_generated_and_anchored(test_db, mocker):
    """A certificate PDF is generated, uploaded, anchored, and recorded on the
    report (s3_url + tx_hash + certificate_url)."""
    from app.core.models import Report

    email = "vpcert+cert@booppa.io"
    user = _make_user(test_db, email, uen="201900000Z")
    rpt = _vp_report(test_db, user.id, uen="201900000Z")

    _run(rpt.id, email, mocker, anchor="0xdeadbeef")

    test_db.expire_all()
    r = test_db.query(Report).filter(Report.id == rpt.id).first()
    assert r.s3_url and r.s3_url.endswith(".pdf")
    assert r.tx_hash == "0xdeadbeef"
    assert r.audit_hash and len(r.audit_hash) == 64
    assert r.assessment_data.get("certificate_url")


def test_certificate_generator_renders_pdf():
    from app.services.vendor_proof_generator import generate_vendor_proof_certificate
    pdf = generate_vendor_proof_certificate(
        company_name="Acme & Sons <Ltd>", uen="201912345A",
        acra_data={"matched": True, "entity_type": "PRIVATE COMPANY"},
        score=78, readiness_label="Ready", verify_url="https://booppa.io/verify/x",
        tx_hash="0xabc", network_name="Polygon Amoy Testnet",
        explorer_url="https://amoy.polygonscan.com",
    )
    assert pdf[:4] == b"%PDF"
    # No-match / no-score path must still render
    pdf2 = generate_vendor_proof_certificate("NoScan Co", None, None, "Identity verified only")
    assert pdf2[:4] == b"%PDF"
