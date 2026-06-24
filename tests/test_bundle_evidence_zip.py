"""Compliance Bundle single evidence ZIP (Sprint 8d)."""
import io
import uuid
import zipfile
from datetime import datetime, timezone

from tests._test_helpers import make_user


def _report(db, user, framework, key, body):
    from app.core.models import Report
    from app.services.storage import S3Service
    s3 = S3Service()
    s3.s3_client.put_object(Bucket=s3.bucket, Key=key, Body=body, ContentType="application/pdf")
    db.add(Report(
        owner_id=user.id,
        framework=framework,
        company_name="Acme",
        assessment_data={"s3_key": key},
        status="completed",
        file_key=key,
        tx_hash=f"0x{uuid.uuid4().hex}",
        completed_at=datetime.now(timezone.utc),
    ))
    db.commit()


def test_zip_bundles_cover_letter_sheet_and_docs(test_db, s3_bucket):
    from app.workers.tasks import _build_compliance_bundle_zip

    user = make_user(test_db, email="bundle-zip@booppa.io", plan="compliance_evidence_pack", company="Acme")
    _report(test_db, user, "pdpa_quick_scan", f"reports/pdpa-{user.id}.pdf", b"%PDF-1.4 pdpa")
    _report(test_db, user, "rfp_complete", f"reports/rfp-{user.id}.pdf", b"%PDF-1.4 rfp")

    result = _build_compliance_bundle_zip(test_db, user.id, "Acme", b"%PDF-1.4 coversheet")
    assert result is not None
    filename, zip_bytes = result
    assert filename.startswith("Booppa_Compliance_Bundle_Acme_")
    assert filename.endswith(".zip")

    names = zipfile.ZipFile(io.BytesIO(zip_bytes)).namelist()
    assert any(n.startswith("00_Cover_Letter") for n in names)   # cover letter
    assert any(n.startswith("Cover_Sheet_Acme") for n in names)  # the generated sheet
    assert any("PDPA_Snapshot" in n for n in names)
    assert any("RFP_Complete_Kit" in n for n in names)


def test_zip_none_when_no_documents(test_db, s3_bucket):
    from app.workers.tasks import _build_compliance_bundle_zip

    user = make_user(test_db, email="bundle-zip2@booppa.io", plan="compliance_evidence_pack", company="Acme")
    # Only a cover sheet, no anchored cycle documents → not worth a ZIP.
    assert _build_compliance_bundle_zip(test_db, user.id, "Acme", b"%PDF-1.4 cs") is None
