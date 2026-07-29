"""Shared dimension→finding synthesis (incident 22fb2871, Cover Sheet leg).

`resolve_pdpa_findings` returns only what the AI persisted, and
`_detect_violations` never reads `trackers` / `policy_clauses` / `hosting` /
`pdpc_enforcement`. So a scan can score several dimensions Non-Compliant while
persisting ZERO findings.

The Quick Scan PDF was fixed to derive findings from its own score table. Every
other consumer stayed blind — most damagingly the Compliance Cover Sheet, which
ships in the same bundle and rendered "No PDPA findings were attached", a
"Minimal" risk level (all severity counts zero) and boilerplate recommendations
for a scan the PDF was simultaneously reporting as failing.

`resolve_pdpa_findings_with_dimensions` is the shared fix. These tests pin both
directions: real gaps must surface, and clean scans must stay clean.
"""
import pytest

from app.services.pdpa_findings import (
    DIMENSION_FINDING_SPEC,
    resolve_pdpa_findings,
    resolve_pdpa_findings_with_dimensions,
)

# Netpoleons shape: 6 pre-consent trackers, no banner. Cookie Consent 8/100 and
# Third-Party Tracker Inventory 30/100 — with no AI findings persisted at all.
_FAILING_NO_FINDINGS = {
    "trackers": {"inventory": ["Google Analytics", "Meta Pixel", "LinkedIn Insight Tag"]},
    "consent_mechanism": {"has_cookie_banner": False},
    "nric": {"status": "Compliant", "score": 100},
}


def test_baseline_resolver_really_is_empty_here():
    """Guards the premise: without synthesis this scan yields zero findings, which
    is what every downstream renderer turned into 'you are clean'."""
    assert resolve_pdpa_findings(_FAILING_NO_FINDINGS) == []


def test_failing_dimensions_become_findings():
    out = resolve_pdpa_findings_with_dimensions(_FAILING_NO_FINDINGS)
    titles = {f["title"] for f in out}

    assert "Cookie Consent Mechanism" in titles
    assert "Third-Party Tracker Inventory" in titles
    # Non-Compliant must carry a severity the Cover Sheet's risk-level ladder
    # and severity_counts can actually see — "Minimal" was the bug.
    assert all(f["severity"] == "HIGH" for f in out)
    assert all(f["derived_from_dimension"] is True for f in out)


def test_derived_findings_carry_what_renderers_read():
    """The Cover Sheet renders severity/title/description/recommendation and ranks
    by severity. A synthesized finding missing these renders as a blank card."""
    out = resolve_pdpa_findings_with_dimensions(_FAILING_NO_FINDINGS)
    f = next(f for f in out if f["title"] == "Cookie Consent Mechanism")

    assert f["type"] == "cookie_consent_violation"
    assert f["description"]
    assert f["requirements"]
    assert f["deadline"] == "7 days"


def test_compliant_dimensions_never_synthesize():
    """A passing dimension must never produce a finding — the inverse failure is
    telling a clean customer they have a gap."""
    out = resolve_pdpa_findings_with_dimensions(_FAILING_NO_FINDINGS)
    assert "NRIC Exposure" not in {f["title"] for f in out}


def test_clean_scan_stays_clean():
    out = resolve_pdpa_findings_with_dimensions({
        "nric": {"status": "Compliant", "score": 100},
        "consent_mechanism": {"has_cookie_banner": True},
    })
    assert out == []


def test_ai_findings_win_over_synthesis():
    """When the AI already wrote a finding for a dimension, we must not duplicate
    it — the buyer would see the same gap twice with different wording."""
    ad = dict(_FAILING_NO_FINDINGS)
    ad["booppa_report"] = {"detailed_findings": [{
        "type": "cookie_consent_violation",
        "title": "No consent banner present",
        "severity": "CRITICAL",
    }]}

    out = resolve_pdpa_findings_with_dimensions(ad)
    cookie_ish = [f for f in out if "cookie" in (f.get("type") or "")]

    assert len(cookie_ish) == 1
    assert cookie_ish[0]["severity"] == "CRITICAL"  # the AI's, not the synthesized HIGH
    # ...but the uncovered tracker dimension must still be filled.
    assert "Third-Party Tracker Inventory" in {f["title"] for f in out}


def test_nested_ai_findings_are_preserved():
    """Synthesis must sit on top of the nesting fix, not replace it."""
    ad = dict(_FAILING_NO_FINDINGS)
    ad["booppa_report"] = {"detailed_findings": [
        {"type": "unrelated_violation", "title": "Something else", "severity": "LOW"}
    ]}
    titles = {f["title"] for f in resolve_pdpa_findings_with_dimensions(ad)}
    assert "Something else" in titles


@pytest.mark.parametrize("dimension", [
    "Cookie Consent Mechanism",
    "Third-Party Tracker Inventory",
    "Privacy Policy (PDPA §11/13)",
    "Retention Limitation (§25)",
    "Data Breach Notification (§26B-D)",
    "Cross-Border Transfer (§26)",
    "NRIC Exposure",
])
def test_every_snapshot_dimension_has_a_spec(dimension):
    """`compute_dimension_snapshots` can emit each of these. Any one missing from
    the spec is silently un-synthesizable — a failing dimension that produces no
    finding, which is the original bug."""
    assert dimension in DIMENSION_FINDING_SPEC


def test_pdf_service_shares_the_same_spec():
    """A second copy of the table next to one renderer is how the Quick Scan PDF
    and the Cover Sheet came to describe the same scan differently."""
    from app.services.pdf_service import PDFService

    assert PDFService._DIMENSION_FINDING_SPEC is DIMENSION_FINDING_SPEC


# ---------------------------------------------------------------------------
# Call-site migration
#
# The shared fix only helps consumers that actually call it. Two recurring
# deliverables were still on the blind resolver a full cycle after 22fb2871 was
# closed: the monthly PDPA Monitor report and the Vendor Pro quarterly PDPA
# snapshot. Both brief the customer from `findings`, so an empty list there is
# the original incident reaching a paying reader again.
#
# The aging/history loop in `run_pdpa_monitor_report_for_user` is deliberately
# NOT included: it computes each finding's `first_seen` from prior scans, and
# synthesising there would retroactively restate "days open" on findings the AI
# never wrote.
# ---------------------------------------------------------------------------
_BRIEFING_FUNCTIONS = (
    "run_pdpa_monitor_report_for_user",
    "run_vendor_pro_pdpa_snapshot_for_user",
)


def _findings_assignment_calls(func_node):
    """Names of the resolver called in `findings = <resolver>(...)` statements."""
    import ast

    names = []
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "findings"
                   for t in node.targets):
            continue
        call = node.value
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
            names.append(call.func.id)
    return names


@pytest.mark.parametrize("func_name", _BRIEFING_FUNCTIONS)
def test_report_briefings_use_the_dimension_aware_resolver(func_name):
    import ast
    import inspect

    from app.workers import tasks

    tree = ast.parse(inspect.getsource(tasks))
    targets = [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == func_name]
    assert targets, f"{func_name} not found — was it renamed?"

    for fn in targets:
        called = _findings_assignment_calls(fn)
        assert called, f"{func_name} no longer assigns `findings` from a resolver"
        assert "resolve_pdpa_findings" not in called, (
            f"{func_name} briefs the customer from the blind resolver; a scan "
            f"with failing dimensions and no AI findings reads as clean "
            f"(incident 22fb2871)"
        )
        assert "resolve_pdpa_findings_with_dimensions" in called


# ---------------------------------------------------------------------------
# Citations
#
# The Monitor briefing builds its allow-list of citable PDPA sections from each
# finding's `legislation` key, then interpolates that list into the model prompt.
# Synthesized findings carried no such key, so the list came back empty and the
# literal fallback string "(none provided)" was rendered into the prompt — and
# echoed back into all three customer-facing bullets:
#   "...referencing PDPA section (none provided), estimated N weeks."
#
# Wording is never invented here: it comes from the curated VIOLATION_LEGISLATION
# map, which is the same source the Quick Scan PDF prints for the same slug.
# ---------------------------------------------------------------------------
def test_every_synthesized_finding_carries_a_citation():
    """An uncitable finding is what put "(none provided)" in a customer email."""
    from app.services.pdpa_findings import synthesize_dimension_finding

    uncitable = []
    for name in DIMENSION_FINDING_SPEC:
        f = synthesize_dimension_finding(name, 30, "Non-Compliant")
        if not (f.get("legislation") or "").strip():
            uncitable.append(name)
    assert not uncitable, f"dimensions with no legislation: {uncitable}"


@pytest.mark.parametrize("dimension,expected", [
    # The two reported by name. Both must match wording already verified
    # elsewhere in the product, not new phrasing.
    ("Security HTTP Headers", "s.24"),
    ("DNC Registry Reference", "Part 9"),
])
def test_reported_dimensions_cite_the_verified_reference(dimension, expected):
    from app.services.pdpa_findings import synthesize_dimension_finding

    f = synthesize_dimension_finding(dimension, 30, "Non-Compliant")
    assert expected in f["legislation"], f["legislation"]


def test_citations_survive_into_the_monitor_allow_list(monkeypatch):
    """End-to-end on the real parser: a briefing built from findings that carry
    no legislation of their own must still cite, never fall back to the
    "(none provided)" prompt string.

    Note the shapes below are AI-finding slugs, not dimension snapshots:
    `compute_dimension_snapshots` emits neither Security HTTP Headers nor DNC
    Registry Reference, so those two dimensions reach the briefing ONLY as
    AI-authored findings — which is exactly the pair that arrived uncited.
    """
    from app.services.booppa_ai_service import BooppaAIService
    from app.workers.tasks import _pdpa_monitor_briefing_bullets

    findings = [
        {"type": "security_headers_violation", "title": "Missing HSTS and CSP",
         "severity": "HIGH"},
        {"type": "dnc_registry_violation", "title": "No DNC screening described",
         "severity": "HIGH"},
        {"type": "data_subject_rights_violation", "title": "No access request path",
         "severity": "MEDIUM"},
    ]

    # Force the deterministic finding-grounded path. Never left to ambient config:
    # if a DeepSeek key happens to be present this test would make a live call and
    # assert on model output.
    async def _no_model(self, messages, *a, **kw):
        return None

    monkeypatch.setattr(BooppaAIService, "_call_deepseek", _no_model)

    bullets = _pdpa_monitor_briefing_bullets("Netpoleons", "ICT", findings, 58, 62)
    assert len(bullets) == 3, bullets
    assert not any("none provided" in b.lower() for b in bullets), bullets
    # Every bullet carries a real reference, not just the absence of the fallback.
    assert all(("s.2" in b or "Part 9" in b or "s.1" in b) for b in bullets), bullets


def test_dnc_part_reference_reaches_the_allow_list_and_survives_validation(monkeypatch):
    """The DNC obligations are cited by Part, not section, in every verified
    reference we hold. A section-only parser dropped them from the allow-list,
    so a correct "PDPA Part 9" bullet was discarded as a hallucination — and a
    scan whose only failing dimension was DNC had nothing left to cite."""
    import json

    from app.services.booppa_ai_service import BooppaAIService
    from app.workers.tasks import _pdpa_monitor_briefing_bullets

    seen = {}

    async def _fake_call(self, messages, *a, **kw):
        seen["system"] = messages[0]["content"]
        return json.dumps([
            "Screen numbers against the DNC Registry before marketing "
            "(PDPA Part 9), estimated 2 weeks.",
            "Set the missing security response headers (§24), estimated 1 week.",
            "Publish a data-subject request pathway, estimated 3 weeks.",
        ])

    monkeypatch.setattr(BooppaAIService, "_call_deepseek", _fake_call)

    findings = [
        {"type": "security_headers_violation", "title": "Missing HSTS and CSP",
         "severity": "HIGH"},
        {"type": "dnc_registry_violation", "title": "No DNC screening described",
         "severity": "HIGH"},
    ]
    bullets = _pdpa_monitor_briefing_bullets("Netpoleons", "ICT", findings, 58, 62)

    # The Part reference must be offered to the model as a citable authority...
    assert "Part 9" in seen["system"], seen["system"]
    assert "§PART" not in seen["system"].upper(), "malformed Part citation"
    assert "(none provided)" not in seen["system"]
    # ...and the bullet that uses it must survive validation.
    assert any("Part 9" in b for b in bullets), bullets


# ---------------------------------------------------------------------------
# Uncitable findings must never reach the model
#
# Phase 3 backfills citations from the curated slug map, but a slug absent from
# that map still yields an empty allow-list — and the prompt then carried the
# literal "(none provided)". _validate() is structurally blind to it: the phrase
# parses to zero sections, so the bullet reads as advisory and is kept. The only
# defence is to refuse to build that prompt at all.
# ---------------------------------------------------------------------------
def test_uncitable_findings_never_reach_the_model(monkeypatch):
    """An unknown slug must skip the model, not prompt it with "(none provided)"."""
    from app.services.booppa_ai_service import BooppaAIService
    from app.workers.tasks import _pdpa_monitor_briefing_bullets

    called = {"n": 0}

    async def _spy(self, messages, *a, **kw):
        called["n"] += 1
        # What the model did before this guard: echo the fallback back verbatim.
        import json
        return json.dumps([
            "Do X, referencing PDPA section (none provided), estimated 2 weeks.",
        ] * 3)

    monkeypatch.setattr(BooppaAIService, "_call_deepseek", _spy)

    findings = [
        {"type": "some_unmapped_future_slug", "title": "Unknown gap", "severity": "HIGH"},
    ]
    bullets = _pdpa_monitor_briefing_bullets("Netpoleons", "ICT", findings, 58, 62)

    assert called["n"] == 0, "model was prompted with an empty allow-list"
    assert not any("none provided" in b.lower() for b in bullets), bullets
    assert bullets, "briefing must not come back empty"


def test_act_only_legislation_prefers_finding_grounded_bullets(monkeypatch):
    """Legislation naming only an Act parses to no section, so the model is
    skipped — but those bullets are still finding-grounded and must be preferred
    over the generic list, which hard-codes §26D regardless of the findings."""
    from app.services.booppa_ai_service import BooppaAIService
    from app.workers.tasks import _GENERIC_MONITOR_BRIEFING, _pdpa_monitor_briefing_bullets

    # Not a raise: the caller wraps the model call in `except Exception`, which
    # would swallow an AssertionError and let this test pass vacuously.
    called = {"n": 0}

    async def _never(self, messages, *a, **kw):
        called["n"] += 1
        return None

    monkeypatch.setattr(BooppaAIService, "_call_deepseek", _never)

    findings = [
        {"type": "unmapped_slug", "title": "Unsolicited marketing messages",
         "severity": "HIGH", "legislation": "Spam Control Act"},
    ]
    bullets = _pdpa_monitor_briefing_bullets("Netpoleons", "ICT", findings, 58, 62)

    assert called["n"] == 0, "model must not be called with an empty allow-list"
    assert bullets != _GENERIC_MONITOR_BRIEFING, bullets
    assert any("Spam Control Act" in b for b in bullets), bullets
    assert not any("26D" in b for b in bullets), bullets


def test_no_none_provided_fallback_remains_in_the_prompt_builder():
    """Static guard: the literal must not be reintroduced as an allow-list default."""
    import inspect
    import app.workers.tasks as tasks

    src = inspect.getsource(tasks._pdpa_monitor_briefing_bullets)
    allow_line = [
        ln for ln in src.splitlines()
        if "allow_str" in ln and "=" in ln and "join" in ln
    ]
    assert allow_line, "allow_str assignment not found"
    assert not any("none provided" in ln for ln in allow_line), allow_line


# ---------------------------------------------------------------------------
# Acceptance test (Gianpaolo, bug report #2 / ITEM 3)
#
#   "regenerate this month's PDPA Monitor and confirm all three bullets cite a
#    real section number, never '(none provided)'... please re-run against
#    Vendor Pro specifically, not just Vendor Active, since both products share
#    these generators."
#
# The reported email had ALL THREE bullets reading
#   "...referencing PDPA section (none provided), estimated N weeks."
# so the acceptance bar is per-bullet, not "at least one cites".
#
# The model stub below is deliberately obedient rather than absent: it echoes
# the prompt's allow-list back, which is exactly what the real model did — and
# is why an empty allow-list leaked the fallback string verbatim. Stubbing the
# model to return None would pass this test even with the bug reinstated.
# ---------------------------------------------------------------------------
_CITATION_RE = r"(?:§|s\.)\s*\d+[A-Z]?|Part\s+\d+"

# The customer's scan shape: dimensions failing, ZERO AI findings persisted
# (incident 22fb2871), plus the two AI-authored slugs that arrived uncited.
_JULY_2026_SCAN = dict(_FAILING_NO_FINDINGS)
_UNCITED_AI_SLUGS = [
    {"type": "security_headers_violation", "title": "Missing HSTS and CSP", "severity": "HIGH"},
    {"type": "dnc_registry_violation", "title": "No DNC screening described", "severity": "HIGH"},
]


def _echoing_model(monkeypatch):
    """Model stub that quotes the prompt's allow-list — the real failure mode."""
    import json
    import re

    from app.services.booppa_ai_service import BooppaAIService

    seen = {}

    async def _call(self, messages, *a, **kw):
        system = next(m["content"] for m in messages if m["role"] == "system")
        allowed = re.search(r"sections you may cite are: (.*?)\. Do not cite", system)
        seen["allow"] = allowed.group(1) if allowed else ""
        first = seen["allow"].split(", ")[0] if seen["allow"] else "(none provided)"
        return json.dumps([
            f"Remediate item {i}, referencing PDPA section {first}, estimated 2 weeks."
            for i in range(1, 4)
        ])

    monkeypatch.setattr(BooppaAIService, "_call_deepseek", _call)
    return seen


@pytest.mark.parametrize("entrypoint", _BRIEFING_FUNCTIONS)
def test_acceptance_every_monitor_bullet_cites_a_real_section(entrypoint, monkeypatch):
    """Both products' findings input, run through the real briefing generator."""
    import re

    from app.workers.tasks import _pdpa_monitor_briefing_bullets

    seen = _echoing_model(monkeypatch)

    # Whichever entrypoint fires, it hands the briefing the output of the shared
    # dimension-aware resolver (pinned by
    # test_report_briefings_use_the_dimension_aware_resolver) — so this is the
    # real payload for Vendor Active and Vendor Pro alike.
    findings = resolve_pdpa_findings_with_dimensions(_JULY_2026_SCAN) + _UNCITED_AI_SLUGS
    assert findings, f"{entrypoint}: premise broken — no findings to brief on"

    bullets = _pdpa_monitor_briefing_bullets("Netpoleons", "ICT", findings, 58, 62)

    assert len(bullets) == 3, bullets
    assert "none provided" not in seen.get("allow", ""), seen
    for b in bullets:
        assert "none provided" not in b.lower(), b
        assert re.search(_CITATION_RE, b), b


def test_acceptance_vendor_pro_findings_are_all_citable():
    """Vendor Pro shares the generators, so the guarantee has to hold at its own
    resolver call site: every finding it can hand the briefing must contribute a
    citation, or the allow-list empties and the model is skipped entirely."""
    from app.services.booppa_ai_service import VIOLATION_LEGISLATION

    findings = resolve_pdpa_findings_with_dimensions(_JULY_2026_SCAN) + _UNCITED_AI_SLUGS
    uncitable = [
        f for f in findings
        if not (f.get("legislation") or "").strip()
        and not VIOLATION_LEGISLATION.get(f.get("type") or "")
    ]
    assert not uncitable, uncitable
