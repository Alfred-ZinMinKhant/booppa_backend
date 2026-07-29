"""Regression: one Vendor Pro activation shipped three different PDPA scores.

The 29 Jul 2026 live re-test delivered, from a single activation:

    Status Snapshot + Pro Report headline   56
    Pro Report "Current PDPA compliance score"   61
    PDPA Monitor Report (current)   58

— two of those inside the same PDF, which
``vendor_pro_report_generator.generate_vendor_pro_report_pdf``'s own docstring
says must never happen again.

Three consumers each ran their own query. They disagreed on:

  * **ordering** — ``created_at desc`` vs ``completed_at desc nullslast``, which
    pick different rows whenever a scan finishes out of creation order;
  * **scoping** — only the headline honoured ``User.website``;
  * **the score ladder** — one canonical ``resolve_pdpa_score``, one hand-rolled
    ``overall_score/compliance_score/score``, one direct ``compliance_score``
    read with a ``100 - risk`` fallback that couldn't see the nested key.

All three now go through ``pdpa_findings.latest_vendor_pdpa_reports``. These
tests pin agreement itself, not any particular number: the invariant is that the
documents cannot disagree, whichever report wins.
"""
from datetime import datetime, timedelta, timezone

from app.services.pdpa_findings import (
    latest_vendor_pdpa_reports,
    resolve_pdpa_score,
    vendor_pdpa_score,
)
from app.services.vendor_active_insights import get_pdpa_drift

SITE = "https://vendor-sso.com.sg"


def _vendor(db, email, website=SITE):
    from app.core.models import User

    u = User(email=email, hashed_password="x", role="VENDOR",
             plan="vendor_pro", company="Vendor Pte Ltd", website=website)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _scan(db, user, score, created_days_ago, completed_days_ago=None,
          website=SITE, assessment_data=None):
    """A completed PDPA scan. `created` and `completed` are set independently so
    a test can invert their orders — the case the two orderings disagreed on."""
    from app.core.models import Report

    now = datetime.now(timezone.utc)
    completed = created_days_ago if completed_days_ago is None else completed_days_ago
    r = Report(
        owner_id=user.id,
        framework="pdpa_quick_scan",
        company_name="Vendor Pte Ltd",
        company_website=website,
        assessment_data=(assessment_data if assessment_data is not None
                         else {"compliance_score": score}),
        status="completed",
        created_at=now - timedelta(days=created_days_ago),
        completed_at=now - timedelta(days=completed),
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _monitor_current_score(db, user):
    """What the Monitor Report / quarterly snapshot render as CURRENT.

    Mirrors the (now shared) selection in ``tasks.py`` without booting Celery,
    S3 or the PDF stack.
    """
    reports = latest_vendor_pdpa_reports(db, user, limit=2)
    return resolve_pdpa_score(reports[0].assessment_data) if reports else None


def _all_three(db, user):
    drift = get_pdpa_drift(db, user) or {}
    return {
        "headline": vendor_pdpa_score(db, user),
        "drift": drift.get("current_score"),
        "monitor": _monitor_current_score(db, user),
    }


# ── The live failure ────────────────────────────────────────────────────────

def test_three_documents_agree_when_scans_complete_out_of_order(test_db):
    """The exact shape that produced 56 / 61 / 58.

    Three scans where the newest *by creation* is not the newest *by
    completion* — a long-running scan queued first but finishing last. Under the
    old code the headline followed ``created_at`` and the Monitor followed
    ``completed_at``, so they named different reports.
    """
    u = _vendor(test_db, "v+single1@booppa.io")
    _scan(test_db, u, 56, created_days_ago=30)
    # Queued before the third scan, but finished after it.
    _scan(test_db, u, 61, created_days_ago=10, completed_days_ago=0)
    _scan(test_db, u, 58, created_days_ago=2, completed_days_ago=1)

    scores = _all_three(test_db, u)
    assert len(set(scores.values())) == 1, f"documents disagree: {scores}"
    # Newest by completion wins — the scan that most recently finished is the
    # honest "current" posture.
    assert scores["headline"] == 61


def test_previous_is_the_runner_up_of_the_same_ordering(test_db):
    """`previous` must be second in the same ordering, not another query's idea.

    Otherwise a Monitor Report can show a delta against a report the headline
    never considered.
    """
    u = _vendor(test_db, "v+single2@booppa.io")
    _scan(test_db, u, 40, created_days_ago=30)
    _scan(test_db, u, 61, created_days_ago=10, completed_days_ago=0)
    _scan(test_db, u, 58, created_days_ago=2, completed_days_ago=1)

    drift = get_pdpa_drift(test_db, u)
    ordered = latest_vendor_pdpa_reports(test_db, u, limit=2)

    assert drift["current_score"] == resolve_pdpa_score(ordered[0].assessment_data) == 61
    assert drift["previous_score"] == resolve_pdpa_score(ordered[1].assessment_data) == 58


def test_agreement_holds_with_a_single_scan(test_db):
    u = _vendor(test_db, "v+single3@booppa.io")
    _scan(test_db, u, 72, created_days_ago=1)

    assert set(_all_three(test_db, u).values()) == {72}
    assert (get_pdpa_drift(test_db, u) or {})["previous_score"] is None


def test_no_scan_reads_not_assessed_everywhere(test_db):
    """Absence must be absence in all three, never a substituted lookup."""
    u = _vendor(test_db, "v+single4@booppa.io")

    assert _all_three(test_db, u) == {"headline": None, "drift": None, "monitor": None}
    assert latest_vendor_pdpa_reports(test_db, u) == []


# ── Scoping now applies to all three, not just the headline ─────────────────

def test_a_clients_scan_is_excluded_from_every_document(test_db):
    """The Monitor Report used to be unscoped, so an agency's client scan could
    reach it even though the headline correctly rejected it."""
    u = _vendor(test_db, "v+single5@booppa.io")
    _scan(test_db, u, 58, created_days_ago=30)
    _scan(test_db, u, 91, created_days_ago=1, website="https://bigclient.com")

    scores = _all_three(test_db, u)
    assert set(scores.values()) == {58}, f"client scan leaked: {scores}"


def test_unattributed_scan_is_accepted_by_every_document(test_db):
    """`ReportRequest.website` is optional; a scan that omitted it is
    unattributed, not contradictory, and must not strand the vendor."""
    u = _vendor(test_db, "v+single6@booppa.io")
    _scan(test_db, u, 64, created_days_ago=1, website=None)

    assert set(_all_three(test_db, u).values()) == {64}


# ── The score ladder ────────────────────────────────────────────────────────

def test_nested_ai_score_resolves_everywhere(test_db):
    """`process_report_task` persists the AI score under
    ``booppa_report.risk_assessment.score``. The Monitor's old fallback read only
    top-level ``risk_score`` / ``risk_assessment.score``, so it saw None here."""
    u = _vendor(test_db, "v+single7@booppa.io")
    _scan(test_db, u, None, created_days_ago=1,
          assessment_data={"booppa_report": {"risk_assessment": {"score": 30}}})

    # 100 - risk
    assert set(_all_three(test_db, u).values()) == {70}


def test_extract_risk_score_sees_the_nested_key(test_db):
    """`compliance_drift._extract_risk_score` feeds the Monitor's risk delta."""
    from app.services.compliance_drift import _extract_risk_score

    u = _vendor(test_db, "v+single8@booppa.io")
    r = _scan(test_db, u, None, created_days_ago=1,
              assessment_data={"booppa_report": {"risk_assessment": {"score": 30}}})

    assert _extract_risk_score(r) == 30.0


def test_scoreless_report_does_not_shadow_a_finished_one(test_db):
    """A row marked completed with nothing persisted must not win — that is the
    D4 "not available" bug, and it must not win in one document only."""
    u = _vendor(test_db, "v+single9@booppa.io")
    _scan(test_db, u, 66, created_days_ago=5)
    _scan(test_db, u, None, created_days_ago=1, assessment_data={})

    assert set(_all_three(test_db, u).values()) == {66}


def test_a_scan_landing_after_the_trigger_is_not_the_baseline(test_db):
    """The 19:29-vs-19:31 defect: a *newer* scan labelled "previous".

    The Monitor Report is woken by a `report_id`. That branch used to pin
    `current` to the triggering scan and take "the newest scoped report that
    isn't it" as the baseline — but when a second scan lands in the same
    activation it is newer than the trigger, so a future scan became the
    baseline. Live output: the Pro Report rendered at 19:29 said "▲ 2 vs last
    scan" (previous 56) while the Monitor rendered at 19:31 said "previous 58,
    no change", off the same history.

    `report_id` is now only a readiness gate; `current`/`previous` come from the
    shared ordering, so `previous` is always strictly older than `current`.
    """
    u = _vendor(test_db, "v+single10@booppa.io")
    _scan(test_db, u, 56, created_days_ago=30, completed_days_ago=30)
    trigger = _scan(test_db, u, 58, created_days_ago=2, completed_days_ago=2)
    # Lands after the scan that woke the Monitor.
    _scan(test_db, u, 62, created_days_ago=1, completed_days_ago=1)

    reports = latest_vendor_pdpa_reports(test_db, u, limit=2)
    current, previous = reports[0], reports[1]

    # The heart of check 1: the Monitor must quote whatever the snapshot and the
    # Pro headline quote. Pinning `current` to the trigger made it quote 58 while
    # they quoted 62 — the same activation, three documents, two numbers.
    assert current.id != trigger.id, "a newer scan must win over the trigger"
    assert resolve_pdpa_score(current.assessment_data) == vendor_pdpa_score(test_db, u) == 62

    # The baseline must precede the report being quoted, never follow it. The old
    # "newest report that isn't current" rule picked the 62 scan as the baseline
    # *for* the 58 trigger, i.e. a delta against the future.
    assert (previous.completed_at or previous.created_at) < (
        current.completed_at or current.created_at
    )
    assert resolve_pdpa_score(previous.assessment_data) == 58

    # ...and the Pro Report's drift line must tell the same story.
    drift = get_pdpa_drift(test_db, u)
    assert drift["current_score"] == 62
    assert drift["previous_score"] == 58
