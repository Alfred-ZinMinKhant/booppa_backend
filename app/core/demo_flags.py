"""Detect admin test-checkout (``test_simulation``) fulfillment.

Admin test checkout (`app/api/admin.py:simulate_purchase`) and real customer
purchases share ONE Polygon Amoy gas wallet. Test runs used to anchor for real
and drained it (the 2026-07-24 out-of-gas outage that failed real buyers). To
stop that, every on-chain anchor reachable from test checkout passes
``demo=<is-test-checkout>`` into ``anchor_evidence``, which then returns a mock
``demo_tx_hash`` instead of spending gas.

``test_simulation`` is set ONLY by ``simulate_purchase`` — never by the real
Stripe webhook — so demo mode can never trigger for a paying customer. It is
persisted three ways, all of which this module recognises:

- onto ``Report.assessment_data["test_simulation"]`` / ``EvidencePack`` rows,
- into the fulfillment ``metadata`` dict (``metadata["test_simulation"]``),
- as the ``admin-sim-*`` prefix on the session id every test dispatch carries.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional


def is_demo_session(session_id: Optional[str]) -> bool:
    """True for the ``admin-sim-*`` session ids minted by simulate_purchase."""
    return bool(session_id) and str(session_id).startswith("admin-sim-")


def is_demo_data(data: Any) -> bool:
    """True if a Report/EvidencePack assessment or metadata dict is a test run."""
    return isinstance(data, Mapping) and bool(data.get("test_simulation"))


def is_demo_anchor(
    *,
    assessment: Any = None,
    metadata: Any = None,
    session_id: Optional[str] = None,
    is_test: Optional[bool] = None,
) -> bool:
    """Resolve whether an anchor should run in demo (no-gas) mode.

    Pass whichever signals a call site has locally; any one being truthy wins.
    """
    return (
        bool(is_test)
        or is_demo_data(assessment)
        or is_demo_data(metadata)
        or is_demo_session(session_id)
    )
