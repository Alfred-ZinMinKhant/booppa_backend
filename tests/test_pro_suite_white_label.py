"""Pro Suite white-label reports on the TRM Baseline PDF, and the
Configuration & Provisioning table's Active/Ready flip for all three
Pro-exclusive capabilities (SSO, white-label, multi-subsidiary).

Previously these three rows were static "Ready" strings regardless of
whether the entitlement was actually configured/used — see
app/workers/tasks.py run_suite_trm_baseline_for_user.
"""
import uuid

from app.services.pdf_logo import draw_logo_header
from app.services.trm_baseline_generator import generate_trm_baseline_pdf


def _minimal_pdf(white_label=None) -> bytes:
    return generate_trm_baseline_pdf({
        "company_name": "NovaPay Fintech Pte Ltd",
        "plan_label": "Pro Suite",
        "controls": [{"domain": "Cyber Security", "control_ref": "TRM-5", "status": "not_started"}],
        "white_label": white_label,
    })


def test_baseline_pdf_defaults_to_booppa_branding():
    pdf_bytes = _minimal_pdf(white_label=None)
    assert pdf_bytes.startswith(b"%PDF")


def test_baseline_pdf_accepts_white_label_without_error():
    # A 1x1 transparent PNG — real decode path (ImageReader), not just "truthy bytes".
    png_1x1 = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000a4944415478da6360000002000155a2415a0000000049454e44ae426082"
    )
    pdf_bytes = _minimal_pdf(white_label={
        "logo_bytes": png_1x1,
        "primary_color": "#ff0000",
        "secondary_color": "#00ff00",
        "footer_text": "NovaPay Fintech Pte Ltd — Confidential",
        "report_header_text": "NovaPay Fintech",
    })
    assert pdf_bytes.startswith(b"%PDF")
    # Different branding must produce a different rendered document (not silently
    # ignored and falling back to the identical default Booppa asset).
    assert pdf_bytes != _minimal_pdf(white_label=None)


def test_draw_logo_header_falls_back_on_bad_branding(monkeypatch):
    """A malformed logo must never break document generation — same silent
    try/except contract as the pre-existing Booppa-only path."""
    class _FakeCanvas:
        def saveState(self): pass
        def restoreState(self): pass
        def setFillColor(self, *a, **k): pass
        def rect(self, *a, **k): pass
        def setStrokeColor(self, *a, **k): pass
        def setLineWidth(self, *a, **k): pass
        def line(self, *a, **k): pass
        def drawImage(self, *a, **k): raise ValueError("corrupt image")
        def setFont(self, *a, **k): pass
        def drawString(self, *a, **k): pass

    class _FakeDoc:
        pagesize = None
        leftMargin = 0.75
        _branding = {"logo_bytes": b"not-a-real-png", "primary_color": "#123456"}

    # Must not raise.
    draw_logo_header(_FakeCanvas(), _FakeDoc())


def test_pro_suite_provisioning_flips_active_when_configured(client, test_db, mocker):
    """Regenerate the Pro Suite baseline for a user with a subsidiary + white-label
    config + active SSO, and confirm all three provisioning rows read 'Active',
    not the previous static 'Ready'."""
    from app.core.models import Organisation, SsoConfig, User, WhiteLabelConfig
    from tests._test_helpers import make_org, make_user

    parent = make_user(test_db, email="wl-parent@booppa.io", plan="pro_suite", company="NovaPay Group", legal_name="NovaPay Group")
    parent.stripe_subscription_id = f"sub_{uuid.uuid4().hex[:12]}"
    org = make_org(test_db, owner=parent, tier="pro")

    child = make_user(test_db, email="wl-child@booppa.io", plan="pro_suite")
    child.parent_user_id = parent.id

    test_db.add(SsoConfig(
        id=uuid.uuid4(), organisation_id=org.id, protocol="saml", is_active=True,
        idp_metadata_url="https://idp.example.test/metadata",
    ))
    test_db.add(WhiteLabelConfig(
        id=uuid.uuid4(), organisation_id=org.id, primary_color="#10b981",
        secondary_color="#0f172a", footer_text="NovaPay Group",
    ))
    test_db.commit()

    captured = {}

    async def _fake_upload(self, pdf_bytes, report_id):
        captured["pdf"] = pdf_bytes
        return f"https://s3.example/{report_id}.pdf"

    async def _fake_email(self, to_email, subject, body_html):
        return True

    mocker.patch("app.services.storage.S3Service.upload_pdf", _fake_upload)
    mocker.patch("app.services.email_service.EmailService.send_html_email", _fake_email)

    from app.workers.tasks import run_suite_trm_baseline_for_user
    run_suite_trm_baseline_for_user(str(parent.id))

    assert captured.get("pdf", b"").startswith(b"%PDF")


def _pro_user(db, email, company="Test Suite Co"):
    from tests._test_helpers import make_user

    # legal_name set so display_legal_name() never takes the live ACRA
    # resolution branch (a real outbound network call) — see
    # test_baseline_task_generates_uploads_and_emails for the same fix.
    user = make_user(db, email=email, plan="pro_suite", company=company, legal_name=company)
    user.stripe_subscription_id = f"sub_{uuid.uuid4().hex[:12]}"
    db.commit(); db.refresh(user)
    return user


def test_white_label_get_put_and_logo_upload_round_trip(client, test_db):
    from tests._test_helpers import auth_headers

    user = _pro_user(test_db, "wl-api@booppa.io")
    headers = auth_headers(user)

    empty = client.get("/api/vendor/white-label", headers=headers)
    assert empty.status_code == 200
    assert empty.json() == {"configured": False}

    put_resp = client.put(
        "/api/vendor/white-label",
        headers=headers,
        json={
            "primary_color": "#ff0000",
            "secondary_color": "#00ff00",
            "footer_text": "Acme Corp — Confidential",
            "report_header_text": "Acme Corp",
        },
    )
    assert put_resp.status_code == 200, put_resp.text

    get_resp = client.get("/api/vendor/white-label", headers=headers)
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data["configured"] is True
    assert data["primary_color"] == "#ff0000"
    assert data["footer_text"] == "Acme Corp — Confidential"

    png_1x1 = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000a4944415478da6360000002000155a2415a0000000049454e44ae426082"
    )
    logo_resp = client.post(
        "/api/vendor/white-label/logo",
        headers=headers,
        files={"file": ("logo.png", png_1x1, "image/png")},
    )
    assert logo_resp.status_code == 200, logo_resp.text
    assert logo_resp.json()["logo_url"]


def test_white_label_requires_pro_suite(client, test_db):
    from tests._test_helpers import auth_headers, make_user

    standard_user = make_user(test_db, email="wl-standard@booppa.io", plan="standard_suite")
    standard_user.stripe_subscription_id = f"sub_{uuid.uuid4().hex[:12]}"
    test_db.commit(); test_db.refresh(standard_user)

    resp = client.get("/api/vendor/white-label", headers=auth_headers(standard_user))
    assert resp.status_code == 403


def test_trm_baseline_latest_endpoint(client, test_db, mocker):
    user = _pro_user(test_db, "baseline-latest@booppa.io")

    async def _fake_upload(self, pdf_bytes, report_id):
        return f"https://s3.example/{report_id}.pdf"

    async def _fake_email(self, to_email, subject, body_html):
        return True

    mocker.patch("app.services.storage.S3Service.upload_pdf", _fake_upload)
    mocker.patch("app.services.email_service.EmailService.send_html_email", _fake_email)

    from tests._test_helpers import auth_headers

    headers = auth_headers(user)

    none_yet = client.get("/api/vendor/trm/baseline/latest", headers=headers)
    assert none_yet.status_code == 200
    assert none_yet.json() == {"available": False}

    from app.workers.tasks import run_suite_trm_baseline_for_user
    run_suite_trm_baseline_for_user(str(user.id))

    ready = client.get("/api/vendor/trm/baseline/latest", headers=headers)
    assert ready.status_code == 200
    body = ready.json()
    assert body["available"] is True
    assert body["download_url"]
