"""Vendor Active / Pro status snapshot deliverable (Phase E of the audit remediation).

These tiers previously delivered only a metrics email; now the monthly health
check renders a one-page snapshot PDF and links it.
"""
from io import BytesIO

from pypdf import PdfReader


def test_snapshot_pdf_renders_scores():
    from app.services.vendor_snapshot_generator import generate_vendor_snapshot_pdf

    pdf = generate_vendor_snapshot_pdf({
        "company_name": "Crayon Singapore",
        "plan_label": "Vendor Pro",
        "trust_score": 72,
        "compliance_score": 68,
        "profile_views_30d": 14,
        "verification_level": "Standard",
    })
    assert pdf.startswith(b"%PDF")
    text = "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf)).pages)
    assert "Vendor Status Snapshot" in text
    assert "Crayon Singapore" in text
    for n in ("72", "68", "14"):
        assert n in text


def test_snapshot_handles_missing_scores_gracefully():
    from app.services.vendor_snapshot_generator import generate_vendor_snapshot_pdf

    pdf = generate_vendor_snapshot_pdf({"company_name": "X", "plan_label": "Vendor Active"})
    assert pdf.startswith(b"%PDF")  # no crash on None scores


def test_first_cycle_digest_is_single_consolidated_welcome(test_db, mocker):
    """On activation, Vendor Pro must receive ONE consolidated welcome digest
    (no separate bare 'Activated' email): scores + snapshot + GeBIZ alerts +
    feature checklist + the included notarization + a 'PDPA report follows' note.
    """
    from app.core.models import User

    captured = {}

    async def fake_upload(self, pdf_bytes, report_id):
        return f"https://s3.example/{report_id}.pdf"

    async def fake_email(self, to_email, subject, body_html):
        captured["subject"] = subject
        captured["body"] = body_html
        return True

    class _Score:
        total_score = 80
        compliance_score = 75

    mocker.patch("app.services.scoring.VendorScoreEngine.update_vendor_score", return_value=_Score())
    mocker.patch("app.services.storage.S3Service.upload_pdf", fake_upload)
    mocker.patch("app.services.email_service.EmailService.send_html_email", fake_email)

    user = User(
        email="vendor+welcome@booppa.io", hashed_password="x", role="VENDOR",
        plan="vendor_pro", company="Crayon Singapore", website="https://example.test",
    )
    test_db.add(user)
    test_db.commit()
    test_db.refresh(user)

    from app.workers.tasks import vendor_active_health_check_task
    vendor_active_health_check_task(str(user.id), user.email, is_first_cycle=True)

    subject = captured["subject"].lower()
    body = captured["body"]
    assert "here's everything included" in subject          # welcome framing, not "monthly digest"
    assert "What your Vendor Pro subscription includes" in body  # feature checklist
    assert "Active badge" in body                            # an evidenced feature
    assert "notarization" in body.lower()                    # Pro's included notarization
    assert "being generated" in body                         # PDPA report-follows note (first cycle)


def test_health_check_links_snapshot_pdf(test_db, mocker):
    """vendor_active_health_check_task must generate + upload the snapshot and
    embed its download link in the monthly email."""
    from app.core.models import User

    captured = {}

    async def fake_upload(self, pdf_bytes, report_id):
        captured["pdf"] = pdf_bytes
        captured["report_id"] = report_id
        return f"https://s3.example/{report_id}.pdf"

    async def fake_email(self, to_email, subject, body_html):
        captured["to"] = to_email
        captured["body"] = body_html
        return True

    class _Score:
        total_score = 80
        compliance_score = 75

    mocker.patch("app.services.scoring.VendorScoreEngine.update_vendor_score", return_value=_Score())
    mocker.patch("app.services.storage.S3Service.upload_pdf", fake_upload)
    mocker.patch("app.services.email_service.EmailService.send_html_email", fake_email)

    user = User(
        email="vendor+snap@booppa.io",
        hashed_password="not-a-real-hash",
        role="VENDOR",
        plan="vendor_pro",
        company="Crayon Singapore",
        website="https://example.test",
    )
    test_db.add(user)
    test_db.commit()
    test_db.refresh(user)

    from app.workers.tasks import vendor_active_health_check_task
    vendor_active_health_check_task(str(user.id), user.email)

    assert captured.get("pdf", b"").startswith(b"%PDF")
    assert captured["report_id"] == f"vendor-snapshot-{user.id}"
    assert captured["to"] == "vendor+snap@booppa.io"
    assert "Download your status snapshot" in captured["body"]
    assert f"https://s3.example/vendor-snapshot-{user.id}.pdf" in captured["body"]
