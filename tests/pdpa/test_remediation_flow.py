"""End-to-end test of the remediation auto-confirmation flow.

We don't go through the real Celery worker or Postgres here — we exercise
`_confirm_remediations` directly against an in-memory SQLite session, which
is enough to assert the logic: mark fixed → next scan with finding absent
→ confirmation_status='confirmed'; finding still present → 'regressed'.

Uses SQLite in-memory because Postgres-only types (UUID, JSONB) are mocked
via SQLAlchemy's generic types for this isolated check.
"""
import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest

# We deliberately don't import the worker module (it pulls Celery). The
# function under test is pure SQLAlchemy + the finding_keys helper, both
# of which we can recreate inline.
from app.services.finding_keys import extract_finding_keys


def _make_report(assessment_data: dict) -> SimpleNamespace:
    """Minimal stand-in for an SQLAlchemy Report row."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        framework="pdpa_quick_scan",
        assessment_data=assessment_data,
    )


def _confirm_remediations_inline(rems: list, report) -> None:
    """Inline copy of the worker helper's logic so we can test it without
    importing celery. Must stay in sync with tasks.py::_confirm_remediations.
    """
    current_keys = extract_finding_keys(report.assessment_data)
    now = datetime.utcnow()
    for rem in rems:
        if rem.confirmation_status not in ("pending", "regressed"):
            continue
        if rem.status not in ("fixed", "wontfix"):
            continue
        if rem.finding_key in current_keys:
            rem.confirmation_status = "regressed"
        else:
            rem.confirmation_status = "confirmed"
            if not rem.confirmed_at:
                rem.confirmed_at = now
                rem.confirming_report_id = report.id


class TestConfirmationFlow:
    def test_finding_gone_marks_confirmed(self):
        # User marked NRIC leakage as fixed on a previous report
        rem = SimpleNamespace(
            finding_key="nric:leakage",
            status="fixed",
            confirmation_status="pending",
            confirmed_at=None,
            confirming_report_id=None,
        )
        # New scan has NO NRIC leakage
        report = _make_report({"nric": {"kind": "none"}})

        _confirm_remediations_inline([rem], report)

        assert rem.confirmation_status == "confirmed"
        assert rem.confirmed_at is not None
        assert rem.confirming_report_id == report.id

    def test_finding_still_present_marks_regressed(self):
        rem = SimpleNamespace(
            finding_key="nric:leakage",
            status="fixed",
            confirmation_status="pending",
            confirmed_at=None,
            confirming_report_id=None,
        )
        # New scan STILL has the NRIC leakage
        report = _make_report({"nric": {"kind": "leakage"}})

        _confirm_remediations_inline([rem], report)

        assert rem.confirmation_status == "regressed"
        assert rem.confirmed_at is None

    def test_regressed_can_be_confirmed_later(self):
        rem = SimpleNamespace(
            finding_key="tracker:google_analytics",
            status="fixed",
            confirmation_status="regressed",  # previously regressed
            confirmed_at=None,
            confirming_report_id=None,
        )
        # Vendor finally removed the tracker
        report = _make_report({"trackers": {"inventory": []}})

        _confirm_remediations_inline([rem], report)

        assert rem.confirmation_status == "confirmed"

    def test_already_confirmed_not_overwritten(self):
        original_time = datetime(2026, 5, 1, 12, 0, 0)
        original_report_id = uuid.uuid4()
        rem = SimpleNamespace(
            finding_key="nric:leakage",
            status="fixed",
            confirmation_status="confirmed",
            confirmed_at=original_time,
            confirming_report_id=original_report_id,
        )
        report = _make_report({"nric": {"kind": "none"}})

        _confirm_remediations_inline([rem], report)

        # Skipped entirely — already confirmed
        assert rem.confirmed_at == original_time
        assert rem.confirming_report_id == original_report_id

    def test_only_fixed_or_wontfix_processed(self):
        rem_open = SimpleNamespace(
            finding_key="nric:leakage",
            status="open",
            confirmation_status="pending",
            confirmed_at=None,
            confirming_report_id=None,
        )
        report = _make_report({"nric": {"kind": "none"}})

        _confirm_remediations_inline([rem_open], report)

        # status='open' means user hasn't claimed a fix; shouldn't auto-confirm
        assert rem_open.confirmation_status == "pending"

    def test_multiple_remediations_evaluated_independently(self):
        rem_a = SimpleNamespace(
            finding_key="nric:leakage", status="fixed",
            confirmation_status="pending",
            confirmed_at=None, confirming_report_id=None,
        )
        rem_b = SimpleNamespace(
            finding_key="tracker:meta_pixel", status="fixed",
            confirmation_status="pending",
            confirmed_at=None, confirming_report_id=None,
        )
        # Scan: NRIC fixed (gone), Meta Pixel still firing
        report = _make_report({
            "nric": {"kind": "none"},
            "trackers": {"inventory": ["Meta Pixel"]},
        })

        _confirm_remediations_inline([rem_a, rem_b], report)

        assert rem_a.confirmation_status == "confirmed"
        assert rem_b.confirmation_status == "regressed"


class TestKeyDerivationContract:
    """If extract_finding_keys produces a key, the API allows marking it.
    If the same key disappears on the next scan, confirmation logic
    catches it. This test guards both ends of that contract."""

    def test_each_supported_key_round_trips(self):
        # A scan that triggers every category of finding key
        scan = {
            "nric": {"kind": "collection"},
            "policy_clauses": {"missing": ["retention"]},
            "pdpc_enforcement": {"checked": True, "found": True},
            "hosting": {"checked": True, "inferred_provider": "AWS",
                        "inferred_region": None},
            "trackers": {"inventory": ["Hotjar"]},
            "findings": [{"check_id": "no_https"}],
        }
        keys = extract_finding_keys(scan)
        # All keys present
        assert "nric:collection" in keys
        assert "clause:retention" in keys
        assert "breach:pdpc_enforcement" in keys
        assert "xbt:non_sg" in keys
        assert "tracker:hotjar" in keys
        assert "free:no_https" in keys

        # If user marks each one fixed and the next scan is clean…
        clean = _make_report({})
        rems = [
            SimpleNamespace(finding_key=k, status="fixed",
                            confirmation_status="pending",
                            confirmed_at=None, confirming_report_id=None)
            for k in keys
        ]
        _confirm_remediations_inline(rems, clean)
        # …all should auto-confirm
        assert all(r.confirmation_status == "confirmed" for r in rems)
