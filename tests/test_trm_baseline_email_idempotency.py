"""TRM baseline email idempotency — regression test for the reported duplicate
"Your MAS TRM Baseline is ready" email.

Same failure class already fixed once for pdpa_monitor_monthly_rescan_task
(app/workers/tasks.py): run_suite_trm_baseline_for_user is bind=True/
max_retries=2 with the whole body wrapped in a broad retry-on-exception, so a
transient failure *after* a successful email send would re-run the task and
resend the identical email. The fix is a Redis NX lock at the very top of the
task — this test proves the lock short-circuits a second invocation before it
even reaches the DB (sidesteps the DB session/idempotency check entirely, so
it isn't sensitive to the test-env DB wiring the way a full end-to-end run
would be).
"""
import uuid

from app.core.cache import cache as _cache
from app.workers import tasks


def test_second_invocation_is_dropped_before_touching_the_db(mocker):
    user_id = str(uuid.uuid4())
    lock_key = f"trm_baseline_email_lock:{user_id}"
    # Belt and suspenders: this key must be free before the test runs.
    _cache.delete(lock_key) if hasattr(_cache, "delete") else None

    load_user_spy = mocker.patch(
        "app.workers.tasks._load_user", wraps=tasks._load_user,
    )

    tasks.run_suite_trm_baseline_for_user(user_id)
    assert load_user_spy.call_count == 1, "first call should proceed to _load_user"

    tasks.run_suite_trm_baseline_for_user(user_id)
    assert load_user_spy.call_count == 1, (
        "second call for the same user_id must be dropped by the idempotency "
        "lock before ever reaching _load_user — this is what prevents the "
        "duplicate email"
    )


def test_different_users_are_not_cross_blocked(mocker):
    """The lock is keyed per-user — one user's send must never block another's."""
    user_a = str(uuid.uuid4())
    user_b = str(uuid.uuid4())

    load_user_spy = mocker.patch(
        "app.workers.tasks._load_user", wraps=tasks._load_user,
    )

    tasks.run_suite_trm_baseline_for_user(user_a)
    tasks.run_suite_trm_baseline_for_user(user_b)
    assert load_user_spy.call_count == 2


def test_bypass_idempotency_allows_test_checkout_resend(mocker):
    """Admin test checkouts pass bypass_idempotency=True so a QA re-run for the
    same email resends the baseline instead of being silently dropped — while
    production activations (bypass_idempotency=False) keep the 24h guard."""
    user_id = str(uuid.uuid4())
    lock_key = f"trm_baseline_email_lock:{user_id}"
    _cache.delete(lock_key) if hasattr(_cache, "delete") else None

    load_user_spy = mocker.patch(
        "app.workers.tasks._load_user", wraps=tasks._load_user,
    )

    tasks.run_suite_trm_baseline_for_user(user_id, bypass_idempotency=True)
    tasks.run_suite_trm_baseline_for_user(user_id, bypass_idempotency=True)
    assert load_user_spy.call_count == 2, (
        "both bypassed invocations must proceed to _load_user — the test-checkout "
        "resend path must not be blocked by the idempotency lock"
    )
