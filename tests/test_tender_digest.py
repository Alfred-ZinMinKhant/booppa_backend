"""Tender Intelligence digest depth (forensic-audit: <40% of features).

The digest previously showed only sector/agency totals. It now also computes,
from the published GeBIZ award history, supplier benchmarking (who wins + average
ticket), per-sector contract-size bands (price history), and award-by-month bid
timing. This verifies those sections render in both the emailed HTML and the PDF.
"""
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO

from pypdf import PdfReader


def _seed_awards(db):
    from app.core.models import GebizAwardHistory

    # Clean slate: the digest computes latest_award across ALL rows, and the
    # table has a unique (tender_no, supplier_name, awarded_date) constraint —
    # so prior committed rows would skew the window or collide on re-run.
    db.query(GebizAwardHistory).delete()
    db.commit()

    base = date(2026, 3, 15)
    # Two suppliers, two sectors, spread across months, varying amounts.
    awards = [
        ("T1", base, "ACME PTE LTD", Decimal("120000"), "IT", "GOVTECH"),
        ("T2", base, "ACME PTE LTD", Decimal("80000"), "IT", "GOVTECH"),
        ("T3", base - timedelta(days=30), "BETA LLP", Decimal("250000"), "IT", "MOH"),
        ("T4", base - timedelta(days=31), "BETA LLP", Decimal("40000"), "CONSTRUCTION", "HDB"),
        ("T5", base - timedelta(days=60), "ACME PTE LTD", Decimal("60000"), "CONSTRUCTION", "HDB"),
    ]
    for tno, d, sup, amt, sec, ent in awards:
        db.add(GebizAwardHistory(
            tender_no=tno, awarded_date=d, supplier_name=sup,
            award_amt=amt, sector=sec, procuring_entity=ent,
        ))
    db.commit()


def _seed_subscriber(db, email):
    from app.core.models import User
    u = User(
        email=email, hashed_password="x", role="VENDOR",
        plan="tender_intelligence", is_active=True,
        company="Crayon Singapore", full_name="Crayon",
    )
    db.add(u); db.commit(); db.refresh(u)
    return u


def test_digest_includes_benchmarking_price_and_timing(test_db, mocker):
    _seed_awards(test_db)
    user = _seed_subscriber(test_db, "ti+digest@booppa.io")

    captured = {}

    async def fake_upload(self, pdf_bytes, report_id):
        captured["pdf"] = pdf_bytes
        return f"https://s3.example/{report_id}.pdf"

    async def fake_email(self, to_email, subject, body_html, *args, **kwargs):
        captured["to"] = to_email
        captured["subject"] = subject
        captured["body"] = body_html
        return True

    mocker.patch("app.services.storage.S3Service.upload_pdf", fake_upload)
    mocker.patch("app.services.email_service.EmailService.send_html_email", fake_email)

    from app.workers.tasks import send_tender_intelligence_digest
    send_tender_intelligence_digest(target_user_id=str(user.id))

    body = captured.get("body", "")
    assert captured.get("to") == "ti+digest@booppa.io"
    # New email sections present
    assert "Top suppliers" in body
    assert "Typical contract size by sector" in body
    assert "Awards by month" in body
    assert "Bid timing" in body
    # Supplier benchmarking surfaced the most-active awardee
    assert "ACME PTE LTD" in body

    # PDF carries the same new sections
    pdf = captured.get("pdf", b"")
    assert pdf.startswith(b"%PDF")
    text = "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf)).pages)
    assert "Top suppliers" in text
    assert "Typical contract size by sector" in text
    assert "bid timing" in text.lower()
