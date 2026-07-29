"""Regression: a first Vendor Pro activation shipped two different compliance figures.

`run_vendor_pro_activation_for_user` used to queue the welcome digest and the
PDPA scan concurrently. The digest renders the status snapshot and the Pro
report, both of which read `vendor_pdpa_score` at render time — so on a
first-ever activation, with no prior report to read, they printed
"N/A — NO PDPA SCAN" while the Monitor Report (which runs the scan itself)
printed a real score minutes later. Verified on a live production purchase:
snapshot N/A, Pro report N/A, Monitor 58.

The fix chains the digest behind the scan. These tests pin both halves of that:
the digest must not fire early, and it must not be *lost* on any path where the
scan drops — a missing activation email is worse than an inconsistent one.
"""
import uuid

import pytest

from app.core.models import User


@pytest.fixture
def vendor(test_db):
    user = User(
        email=f"vpro-{uuid.uuid4().hex[:8]}@booppa.test",
        hashed_password="not-a-real-hash",
        role="VENDOR",
        plan="vendor_pro",
        website="https://vendor.example",
    )
    test_db.add(user)
    test_db.commit()
    test_db.refresh(user)
    return user


# ── The wrapper: chain, don't fan out concurrently ──────────────────────────

def test_digest_is_not_dispatched_concurrently_with_the_scan(vendor, mocker):
    """The original defect: both queued at once, digest renders before scan lands."""
    from app.workers import tasks

    direct = mocker.patch.object(tasks.vendor_active_health_check_task, "delay")
    delayed = mocker.patch.object(tasks.vendor_active_health_check_task, "apply_async")
    scan = mocker.patch.object(tasks.pdpa_monitor_monthly_rescan_task, "delay")

    tasks.run_vendor_pro_activation_for_user(str(vendor.id))

    scan.assert_called_once()
    # The immediate, unchained dispatch is exactly what produced the N/A.
    direct.assert_not_called()
    # A safety net is scheduled, but deferred — not concurrent.
    assert delayed.call_args.kwargs["countdown"] > 0


def test_scan_receives_the_chain_token(vendor, mocker):
    from app.workers import tasks

    mocker.patch.object(tasks.vendor_active_health_check_task, "apply_async")
    scan = mocker.patch.object(tasks.pdpa_monitor_monthly_rescan_task, "delay")

    tasks.run_vendor_pro_activation_for_user(str(vendor.id))

    assert scan.call_args.kwargs["then_health_check"]


def test_chain_token_is_shared_so_exactly_one_dispatch_wins(vendor, mocker):
    from app.workers import tasks

    delayed = mocker.patch.object(tasks.vendor_active_health_check_task, "apply_async")
    scan = mocker.patch.object(tasks.pdpa_monitor_monthly_rescan_task, "delay")

    tasks.run_vendor_pro_activation_for_user(str(vendor.id))

    assert (
        scan.call_args.kwargs["then_health_check"]
        == delayed.call_args.kwargs["kwargs"]["dedupe_key"]
    )


def test_each_activation_mints_a_fresh_token(vendor, mocker):
    """A QA force_resend re-activation must not be blocked by the last run's key."""
    from app.workers import tasks

    mocker.patch.object(tasks.vendor_active_health_check_task, "apply_async")
    scan = mocker.patch.object(tasks.pdpa_monitor_monthly_rescan_task, "delay")

    tasks.run_vendor_pro_activation_for_user(str(vendor.id))
    tasks.run_vendor_pro_activation_for_user(str(vendor.id))

    tokens = {c.kwargs["then_health_check"] for c in scan.call_args_list}
    assert len(tokens) == 2


def test_no_website_still_dispatches_the_digest_immediately(test_db, mocker):
    """With no website no scan ever runs, so there is nothing to wait for."""
    from app.workers import tasks

    user = User(
        email=f"nosite-{uuid.uuid4().hex[:8]}@booppa.test",
        hashed_password="not-a-real-hash",
        role="VENDOR",
        plan="vendor_pro",
        website=None,
    )
    test_db.add(user)
    test_db.commit()
    test_db.refresh(user)

    direct = mocker.patch.object(tasks.vendor_active_health_check_task, "delay")
    scan = mocker.patch.object(tasks.pdpa_monitor_monthly_rescan_task, "delay")

    tasks.run_vendor_pro_activation_for_user(str(user.id))

    direct.assert_called_once()
    scan.assert_not_called()


# ── The dedupe guard on the digest task itself ──────────────────────────────

def test_dedupe_key_claims_before_doing_any_work(vendor, mocker):
    """The losing dispatch must return before touching scoring/S3/email.

    Asserted by making the Redis claim fail (as it does for the second caller)
    and checking the task exits without recalculating the score — the first
    thing the real body does. Running the full body here would fan out to S3,
    Resend and the AI provider.
    """
    from app.workers import tasks

    redis = mocker.MagicMock()
    redis.set.return_value = False  # someone else already claimed this activation
    mocker.patch.object(type(tasks.celery_app.backend), "client", redis)
    engine = mocker.patch("app.services.scoring.VendorScoreEngine")

    tasks.vendor_active_health_check_task(
        str(vendor.id), vendor.email, None,
        is_first_cycle=True, dedupe_key="vendorpro:tok",
    )

    engine.update_vendor_score.assert_not_called()
    claimed = [c for c in redis.set.call_args_list if "vendor_active_digest_once" in str(c)]
    assert claimed, "guard did not attempt a once-only claim"
    assert claimed[0].kwargs.get("nx") is True, "claim must be atomic (SET NX)"


# ── force_resend must reach the scan-level guards, not just the sub claim ───

def test_force_rescan_is_threaded_to_the_scan_task(vendor, mocker):
    """Clearing `sub_activated:` alone left the rescan's own guards standing.

    The Monitor Report, drift check and quarterly snapshot are all queued from
    *inside* the rescan, so when it dropped, a same-day QA re-test delivered the
    welcome digest and nothing else. Reproduced live: the re-run set contained
    every Vendor Pro PDF except the PDPA Monitor Report.
    """
    from app.workers import tasks

    mocker.patch.object(tasks.vendor_active_health_check_task, "apply_async")
    scan = mocker.patch.object(tasks.pdpa_monitor_monthly_rescan_task, "delay")

    tasks.run_vendor_pro_activation_for_user(str(vendor.id), force_rescan=True)

    assert scan.call_args.kwargs["force_rescan"] is True


def test_force_rescan_defaults_off(vendor, mocker):
    """Production renewals must keep both guards; this is QA-only."""
    from app.workers import tasks

    mocker.patch.object(tasks.vendor_active_health_check_task, "apply_async")
    scan = mocker.patch.object(tasks.pdpa_monitor_monthly_rescan_task, "delay")

    tasks.run_vendor_pro_activation_for_user(str(vendor.id))

    assert scan.call_args.kwargs["force_rescan"] is False


def test_forced_run_takes_a_distinct_lock_key(vendor, mocker):
    """Forcing must not disarm the lock for everyone else.

    A forced run gets its own nonce-suffixed key, so the same-day race stays
    closed for normal callers and two concurrent forced runs cannot collide.
    """
    from app.workers import tasks

    seen = []

    def _add(key, *a, **kw):
        seen.append(key)
        return True

    mocker.patch("app.core.cache.cache.add", side_effect=_add)
    mocker.patch("app.workers.tasks.asyncio.run", return_value="Vendor Example")
    mocker.patch.object(tasks.run_pdpa_monitor_report_for_user, "apply_async")
    mocker.patch.object(tasks.check_compliance_drift_task, "apply_async")
    mocker.patch.object(tasks.run_vendor_pro_pdpa_snapshot_for_user, "apply_async")

    tasks.pdpa_monitor_monthly_rescan_task(
        str(vendor.id), vendor.email, "https://vendor.example",
        source="vendor_pro", force_rescan=True,
    )
    tasks.pdpa_monitor_monthly_rescan_task(
        str(vendor.id), vendor.email, "https://vendor.example",
        source="vendor_pro", force_rescan=True,
    )

    forced = [k for k in seen if ":force:" in k]
    assert len(forced) == 2, "forced runs should still take a claim, not skip it"
    assert forced[0] != forced[1], "two forced runs must not share a lock key"


def test_absent_dedupe_key_does_not_gate_the_monthly_path(vendor, mocker):
    """The guard is opt-in; monthly/Vendor Active callers pass no key.

    A first-cycle call with no key must fall through to the body rather than
    being blocked, so Vendor Active activation is unaffected by this change.
    """
    from app.workers import tasks

    redis = mocker.MagicMock()
    redis.set.return_value = False
    mocker.patch.object(type(tasks.celery_app.backend), "client", redis)
    engine = mocker.patch("app.services.scoring.VendorScoreEngine")
    engine.update_vendor_score.side_effect = RuntimeError("reached the body")

    with pytest.raises(RuntimeError, match="reached the body"):
        tasks.vendor_active_health_check_task(
            str(vendor.id), vendor.email, None, is_first_cycle=True
        )

    assert not [c for c in redis.set.call_args_list if "vendor_active_digest_once" in str(c)]
