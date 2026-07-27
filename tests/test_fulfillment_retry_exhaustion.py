"""Post-payment tasks must not die quietly when their retries run out.

Every branch reached by these tasks runs AFTER the customer's money was taken.
A task that exhausts `max_retries` and raises `MaxRetriesExceededError` produces
a Celery log line and nothing else — which is how a stranded PDPA Quick Scan sat
undelivered while ops saw one alert and then silence. `_retry_or_alert`
centralises the loud-failure path; these tests pin it.
"""
import pytest
from celery.exceptions import MaxRetriesExceededError, Retry

from app.workers import tasks as tasks_mod


class _FakeRequest:
    def __init__(self, retries):
        self.retries = retries


class _FakeTask:
    """Stands in for a bound Celery task without a broker."""

    max_retries = 3

    def __init__(self, *, exhausted: bool):
        self.request = _FakeRequest(3 if exhausted else 0)
        self._exhausted = exhausted
        self.retry_calls = []

    def retry(self, exc=None, countdown=None):
        self.retry_calls.append(countdown)
        if self._exhausted:
            raise MaxRetriesExceededError()
        return Retry()


@pytest.fixture
def alerts(mocker):
    captured = []

    async def _fake_alert(**kwargs):
        captured.append(kwargs)

    mocker.patch(
        "app.services.fulfillment.helpers._alert_payment_fulfillment_issue",
        new=_fake_alert,
    )
    return captured


def test_transient_failure_still_just_retries(alerts):
    task = _FakeTask(exhausted=False)

    with pytest.raises(Retry):
        tasks_mod._retry_or_alert(
            task, RuntimeError("boom"), product_type="pdpa_quick_scan"
        )

    assert task.retry_calls == [60]
    assert alerts == [], "a retryable failure must not page ops"


def test_exhaustion_alerts_ops_and_reraises(alerts):
    task = _FakeTask(exhausted=True)

    with pytest.raises(MaxRetriesExceededError):
        tasks_mod._retry_or_alert(
            task,
            RuntimeError("boom"),
            product_type="pdpa_quick_scan",
            customer_email="buyer@booppa.io",
            session_id="cs_test_1",
            extra={"report_id": "r-1"},
        )

    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["product_type"] == "pdpa_quick_scan"
    assert alert["customer_email"] == "buyer@booppa.io"
    assert alert["session_id"] == "cs_test_1"
    assert alert["extra"]["report_id"] == "r-1"
    assert alert["extra"]["retries"] == 3
    assert "permanently failed" in alert["reason"]
    assert "RuntimeError: boom" in alert["reason"]


def test_exhaustion_reason_differs_from_the_in_flight_alert(alerts):
    """The alert dedupe key includes `reason`.

    If exhaustion reused the wording the fulfillment body already alerted with,
    the hourly suppression would swallow it and ops would never learn the
    purchase was abandoned.
    """
    task = _FakeTask(exhausted=True)
    in_flight_reason = (
        "PDPA fulfillment raised exception: Exception: PDPA scan not yet "
        "complete, retrying later"
    )

    with pytest.raises(MaxRetriesExceededError):
        tasks_mod._retry_or_alert(
            task, Exception("PDPA scan not yet complete, retrying later"),
            product_type="pdpa_quick_scan",
        )

    assert alerts[0]["reason"] != in_flight_reason


def test_on_exhausted_cleanup_runs_before_the_alert(mocker):
    """The RFP path uses this to unblock the buyer's polling result page."""
    task = _FakeTask(exhausted=True)
    order = []

    async def _record_alert(**kwargs):
        order.append("alert")

    mocker.patch(
        "app.services.fulfillment.helpers._alert_payment_fulfillment_issue",
        new=_record_alert,
    )

    with pytest.raises(MaxRetriesExceededError):
        tasks_mod._retry_or_alert(
            task, RuntimeError("boom"),
            product_type="rfp_complete",
            on_exhausted=lambda: order.append("cleanup"),
        )

    assert order == ["cleanup", "alert"]


def test_a_failing_cleanup_cannot_suppress_the_alert(alerts):
    task = _FakeTask(exhausted=True)

    def cleanup():
        raise ValueError("cache down")

    with pytest.raises(MaxRetriesExceededError):
        tasks_mod._retry_or_alert(
            task, RuntimeError("boom"),
            product_type="rfp_complete",
            on_exhausted=cleanup,
        )

    assert len(alerts) == 1, "cleanup failure must not swallow the ops alert"


def test_every_post_payment_task_routes_through_the_helper():
    """Guard against a new fulfillment task reintroducing a bare self.retry."""
    import inspect
    import re

    src = inspect.getsource(tasks_mod)
    for name in (
        "fulfill_pdpa_task",
        "fulfill_notarization_task",
        "fulfill_vendor_proof_task",
        "fulfill_bundle_task",
        "fulfill_rfp_task",
        "activate_subscription_task",
    ):
        body = re.split(rf"^def {name}\(", src, flags=re.M)[1]
        body = re.split(r"^@celery_app\.task", body, flags=re.M)[0]
        assert "_retry_or_alert" in body, f"{name} must alert on retry exhaustion"
