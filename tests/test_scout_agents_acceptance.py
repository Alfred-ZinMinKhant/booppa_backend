"""
tests/test_scout_agents_acceptance.py
Acceptance tests for SCOUT Agents integration:
  1. Vendor scoring task run -> ScoutProspect rows created.
  2. Mark prospect APPROVED -> run send task -> status moves to SENT.
  3. Existing customer exclusion -> UEN match flips status to EXISTING_CUSTOMER.
  4. CSP seed upload pipeline end-to-end run.
"""

import pytest
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, AsyncMock, MagicMock

from app.core.config import settings
from app.core.models import ScoutProspect, User
from app.services.email_suppression import unsubscribe_url
from app.workers.scout_celery_tasks import (
    scout_send_approved_batch_task,
    get_daily_send_cap,
    _upsert_prospects,
)
from app.services.scout_vendor_pipeline import run_vendor_pipeline
from app.services.scout_csp_pipeline import run_csp_pipeline


SEND_TEST_RECIPIENT = "outreach-test@example.com"
TEST_POSTAL_ADDRESS = "Booppa Smart Care LLC, Singapore (test address)"


@pytest.fixture
def postal_address(monkeypatch):
    """COMPANY_POSTAL_ADDRESS is empty by default and the send task fails
    closed on it, so any test that expects a send must set one."""
    monkeypatch.setattr(settings, "COMPANY_POSTAL_ADDRESS", TEST_POSTAL_ADDRESS)
    return TEST_POSTAL_ADDRESS


@pytest.fixture(autouse=True)
def setup_scout_tables(test_db):
    """Ensure ScoutProspect and User tables exist in test database."""
    ScoutProspect.__table__.create(bind=test_db.get_bind(), checkfirst=True)
    User.__table__.create(bind=test_db.get_bind(), checkfirst=True)
    yield


def test_acceptance_1_vendor_scoring(test_db):
    """1. Run vendor scoring pipeline & confirm ScoutProspect rows created in test_db."""
    mock_records = [
        {
            "supplier_name": "NEC ASIA PACIFIC PTE LTD",
            "awarded_amt": "150000",
            "award_date": "2026-01-15",
            "tender_description": "IT infrastructure software systems",
            "uen": "201201936C",
        },
        {
            "supplier_name": "CRIMSONLOGIC PTE LTD",
            "awarded_amt": "250000",
            "award_date": "2026-02-10",
            "tender_description": "Digital platform cyber solutions",
            "uen": "198800784N",
        },
    ]

    async def mock_fetch_page(client, dataset_id, offset, limit=100, filters=None, q=None):
        if "acde1106" in dataset_id:
            return {"result": {"records": mock_records, "total": len(mock_records)}}
        req_uen = (filters or {}).get("uen", "201201936C")
        return {"result": {"records": [{"uen": req_uen, "entity_status_description": "Live Company"}], "total": 1}}

    async def mock_site(name, *args, **kwargs):
        return "https://example-vendor.com"

    async def dummy_sleep(*args, **kwargs):
        pass

    mock_client_instance = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.text = "<html><body>Privacy Policy Cookie Consent DPO Contact dpo@example-vendor.com</body></html>"
    mock_resp.status_code = 200
    mock_client_instance.get.return_value = mock_resp
    mock_client_instance.__aenter__.return_value = mock_client_instance

    with patch("app.services.scout_shared.fetch_datastore_page", side_effect=mock_fetch_page), \
         patch("app.services.scout_shared.find_website_heuristic", side_effect=mock_site), \
         patch("app.services.scout_vendor_pipeline.find_website_heuristic", side_effect=mock_site), \
         patch("app.services.scout_vendor_pipeline.check_tls_honestly") as mock_tls, \
         patch("app.services.scout_vendor_pipeline.httpx.AsyncClient", return_value=mock_client_instance), \
         patch("app.services.scout_shared.httpx.AsyncClient", return_value=mock_client_instance), \
         patch("app.services.scout_vendor_pipeline.asyncio.sleep", side_effect=dummy_sleep), \
         patch("app.services.scout_shared.asyncio.sleep", side_effect=dummy_sleep):

        mock_tls.return_value = {"https_used": True, "handshake_ok": True, "days_until_expiry": 120, "tls_version": "TLSv1.3"}

        scored = asyncio.run(run_vendor_pipeline(limit_vendors=5, min_awards=1))

    assert isinstance(scored, list)
    assert len(scored) > 0, "Vendor pipeline should return candidates"

    new_count, updated_count, excluded_existing = _upsert_prospects(test_db, "vendor", scored)
    assert new_count + updated_count > 0

    prospects = test_db.query(ScoutProspect).filter(ScoutProspect.pipeline == "vendor").all()
    assert len(prospects) > 0
    p = prospects[0]
    assert p.display_name
    assert p.status in ("NEW", "PENDING_APPROVAL", "BELOW_THRESHOLD", "EXISTING_CUSTOMER")


def test_acceptance_2_approval_and_send(test_db, postal_address):
    """2. Mark row APPROVED -> run send task -> confirm row moves to SENT."""
    now = datetime.now(timezone.utc)
    test_key = f"TEST_APPROVAL_KEY_{int(now.timestamp())}"

    prospect = ScoutProspect(
        pipeline="vendor",
        natural_key=test_key,
        display_name="Test Approval Vendor Pte Ltd",
        uen="202699999X",
        website_url="https://example.com",
        score=45,
        priority_tier="TIER1",
        status="APPROVED",
        # Must be a *compliant* message: the send task refuses to dispatch
        # anything lacking the <ADV> prefix, an in-body unsubscribe link for
        # this recipient, or the sender's postal address (Spam Control Act,
        # Cap. 311A Sch 2 — see tests/test_scout_outreach_compliance.py).
        outreach_subject="<ADV> Test Subject",
        outreach_body_html=(
            f'<p>Test Body</p>'
            f'<a href="{unsubscribe_url(SEND_TEST_RECIPIENT)}">unsubscribe</a>'
            f'<p>{TEST_POSTAL_ADDRESS}</p>'
        ),
        raw_data={"contact_email": SEND_TEST_RECIPIENT},
        first_scored_at=now,
        last_scored_at=now,
        created_at=now,
        updated_at=now,
    )
    test_db.add(prospect)
    test_db.commit()
    prospect_id = prospect.id

    with patch("app.workers.scout_celery_tasks.SessionLocal", return_value=test_db), \
         patch("app.workers.scout_celery_tasks.EmailService.send_html_email", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True

        scout_send_approved_batch_task()

    p = test_db.query(ScoutProspect).filter_by(id=prospect_id).first()
    assert p is not None
    assert p.status == "SENT"
    assert p.sent_at is not None
    assert p.send_attempts == 1


def test_acceptance_3_existing_customer_exclusion(test_db):
    """3. Create User row matching UEN -> re-run scoring -> prospect flips to EXISTING_CUSTOMER."""
    test_uen = f"TESTUEN{int(datetime.now(timezone.utc).timestamp())}"

    prospect_item = {
        "clean_name": "Existing Customer Pte Ltd",
        "uen": test_uen,
        "natural_key": test_uen,
        "priority_tier": "TIER1",
        "trust_score": 40,
        "website_url": "https://existingcustomer.com",
        "contact_email": "customer@existingcustomer.com",
    }

    _upsert_prospects(test_db, "vendor", [prospect_item])
    p1 = test_db.query(ScoutProspect).filter(ScoutProspect.natural_key == test_uen).first()
    assert p1.status == "PENDING_APPROVAL"

    test_user = User(
        email=f"user_{test_uen}@example.com",
        hashed_password="hashed_pass_test",
        full_name="Test Customer Owner",
        uen=test_uen,
    )
    test_db.add(test_user)
    test_db.commit()

    _upsert_prospects(test_db, "vendor", [prospect_item])
    p2 = test_db.query(ScoutProspect).filter(ScoutProspect.natural_key == test_uen).first()
    assert p2.status == "EXISTING_CUSTOMER"
    assert "matched an active Booppa account" in p2.status_reason


def test_acceptance_4_csp_seed_upload_pipeline(test_db):
    """4. Seed CSP companies -> run CSP pipeline -> confirm scored prospects produced."""
    seed_list = [
        {"name": "Corporate Services Alpha Pte Ltd", "uen": "202511111A", "licence_issue_date": "2025-01-10"},
        {"name": "Singapore Management Services Pte Ltd", "uen": "202522222B", "licence_issue_date": "2024-05-15"},
    ]

    mock_client_instance = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.text = "<html><body>Anti-Money Laundering AML/CFT Programme Customer Due Diligence CDD</body></html>"
    mock_resp.status_code = 200
    mock_client_instance.get.return_value = mock_resp
    mock_client_instance.__aenter__.return_value = mock_client_instance

    async def dummy_sleep(*args, **kwargs):
        pass

    async def mock_site(name, *args, **kwargs):
        return "https://example-csp.com"

    with patch("app.services.scout_csp_pipeline.find_website_heuristic", side_effect=mock_site), \
         patch("app.services.scout_csp_pipeline.httpx.AsyncClient", return_value=mock_client_instance), \
         patch("app.services.scout_shared.httpx.AsyncClient", return_value=mock_client_instance), \
         patch("asyncio.sleep", side_effect=dummy_sleep):

        scored = asyncio.run(run_csp_pipeline(seed_list))

    assert len(scored) == 2

    new_count, updated_count, _ = _upsert_prospects(test_db, "csp", scored)
    assert new_count + updated_count == 2

    csp_prospects = test_db.query(ScoutProspect).filter(ScoutProspect.pipeline == "csp").all()
    assert len(csp_prospects) >= 2


# ── Send-volume ramp ────────────────────────────────────────────────────────

def _sent_prospect(days_ago: int, idx: int) -> ScoutProspect:
    now = datetime.now(timezone.utc)
    ts = now - timedelta(days=days_ago)
    return ScoutProspect(
        pipeline="vendor",
        natural_key=f"RAMP_{int(now.timestamp())}_{idx}",
        display_name=f"Ramp Fixture {idx}",
        status="SENT",
        sent_at=ts,
        first_scored_at=ts,
        last_scored_at=ts,
        created_at=ts,
        updated_at=ts,
    )


def test_send_cap_no_history_starts_at_floor(test_db):
    assert get_daily_send_cap(test_db) == 5


def test_send_cap_ramps_over_first_month(test_db):
    # Continuous sending since 30 days ago (no gap larger than SEND_RAMP_GAP_DAYS).
    for i, d in enumerate([30, 25, 20, 15, 10, 5, 0]):
        test_db.add(_sent_prospect(d, i))
    test_db.commit()
    assert get_daily_send_cap(test_db) == 60


def test_send_cap_restarts_ramp_after_long_pause(test_db):
    """The case Gianpaolo's email claims: paused, then resumed -> cold start.

    Only ancient sends exist, so the gap is measured against now. Without that
    check this returned 60 on the first day back.
    """
    for i, d in enumerate([200, 195, 190]):
        test_db.add(_sent_prospect(d, i))
    test_db.commit()
    assert get_daily_send_cap(test_db) == 5


def test_send_cap_anchors_on_current_streak_not_first_ever_send(test_db):
    """Old history plus a fresh streak -> ramp reflects the fresh streak only."""
    for i, d in enumerate([200, 195, 190]):   # old, abandoned run
        test_db.add(_sent_prospect(d, i))
    for i, d in enumerate([3, 1, 0], start=10):  # resumed 3 days ago
        test_db.add(_sent_prospect(d, i))
    test_db.commit()
    # Anchor is the send 3 days ago, so we are early in the ramp, not at 60.
    assert get_daily_send_cap(test_db) == 10
