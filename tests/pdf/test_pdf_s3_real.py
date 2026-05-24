"""Real-AWS round-trip test: generate a PDPA PDF and upload it to the real
`booppa-reports` bucket under `test/<run-id>/`. The `real_s3` fixture deletes
every uploaded key on teardown; a 1-day lifecycle rule on the `test/` prefix
catches any leftover from a crashed run.

Skipped automatically if AWS credentials aren't configured."""
import asyncio
import uuid
import pytest

# NOTE: deliberately NOT using @freeze_time here. Frozen time bleeds into
# botocore's request signing, which makes AWS reject S3 PutObject with
# `RequestTimeTooSkewed`. PDF content determinism isn't asserted in this
# round-trip test — content checks live in test_pdf_pdpa.py.


def test_pdpa_pdf_uploads_to_real_s3_and_cleans_up(real_s3):
    from app.services.pdf_service import PDFService
    from app.services.storage import S3Service

    pdf = PDFService().generate_pdf({
        "framework": "pdpa_quick_scan",
        "company_name": "Real S3 Round-Trip Co",
        "created_at": "2026-05-24T12:00:00Z",
        "risk_score": 17,
        "findings": [],
    })
    assert pdf.startswith(b"%PDF")

    report_id = f"unit-{uuid.uuid4().hex[:8]}"
    url = asyncio.run(S3Service().upload_pdf(pdf, report_id))

    assert url.startswith("https://")
    assert real_s3["uploaded_keys"], "fixture did not track the upload"
    # The tracked key should sit under the per-run prefix.
    key = real_s3["uploaded_keys"][-1]
    assert key.startswith(f"reports/{real_s3['prefix']}")

    # Object should exist now…
    head = real_s3["client"].head_object(Bucket=real_s3["bucket"], Key=key)
    assert head["ContentType"] == "application/pdf"
    # …and the fixture's teardown will delete it after this test returns.
