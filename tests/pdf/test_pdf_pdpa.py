"""PDPA Quick Scan PDF content checks.

PDFs are non-deterministic across pdf-lib versions, so we parse with pypdf and
assert the rendered TEXT contains the company name and framework heading rather
than diffing bytes.
"""
from io import BytesIO

import pytest
from freezegun import freeze_time


@freeze_time("2026-05-24T12:00:00Z")
def test_pdpa_quick_scan_pdf_has_company_and_framework():
    from app.services.pdf_service import PDFService

    pdf_bytes = PDFService().generate_pdf({
        "framework": "pdpa_quick_scan",
        "company_name": "Acme Test Co",
        "created_at": "2026-05-24T12:00:00Z",
        "risk_score": 42,
        "findings": [
            {"category": "Cookie consent", "severity": "high", "summary": "no banner"},
        ],
    })

    assert pdf_bytes.startswith(b"%PDF"), "not a PDF"
    assert len(pdf_bytes) > 1500, "PDF suspiciously small"

    from pypdf import PdfReader
    reader = PdfReader(BytesIO(pdf_bytes))
    text = "\n".join(p.extract_text() or "" for p in reader.pages)

    assert "Acme Test Co" in text
    # A one-time PDPA scan (no Monitor trigger source) is branded "PDPA Snapshot"
    # on both the cover subtitle and the running header band — never a plain
    # "Quick Scan". A Monitor rescan (triggered_by=pdpa_monitor_*) reads "PDPA Monitor".
    assert "PDPA SNAPSHOT" in text.upper()
    assert "QUICK SCAN" not in text.upper()


@pytest.mark.parametrize(
    "triggered_by, expect_label",
    [
        ("pdpa_monitor_monthly", "PDPA MONITOR"),
        ("pdpa_monitor_annual", "PDPA MONITOR"),
        ("vendor_pro", "PDPA SNAPSHOT"),
        (None, "PDPA SNAPSHOT"),
    ],
)
def test_pdpa_pdf_branding_by_trigger_source(triggered_by, expect_label):
    """The underlying pdpa_quick_scan PDF is branded by its trigger source so a
    PDPA Monitor subscriber's deliverable never reads as a plain Quick Scan, and
    a Vendor Pro / one-time scan is not mislabeled as Monitor. Regression guard
    for the billing-integrity report: assessment_data['triggered_by'] must reach
    the PDF (single_products._fulfill_pdpa) and drive both the cover subtitle and
    the running header band (pdf_service.generate_pdf)."""
    from app.services.pdf_service import PDFService
    from pypdf import PdfReader

    data = {
        "framework": "pdpa_quick_scan",
        "company_name": "Acme Test Co",
        "created_at": "2026-05-24T12:00:00Z",
        "risk_score": 42,
        "findings": [],
    }
    if triggered_by is not None:
        data["assessment_data"] = {"triggered_by": triggered_by}

    pdf_bytes = PDFService().generate_pdf(data)
    text = "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf_bytes)).pages).upper()

    # The deliverable's label surfaces (cover subtitle + running header band)
    # carry expect_label; a plain "Quick Scan" must never appear.
    assert expect_label in text
    assert "QUICK SCAN" not in text
    # A non-Monitor scan must never be branded "PDPA Monitor". (The reverse isn't
    # asserted: a Monitor PDF legitimately uses the word "snapshot" in generic
    # descriptive prose, e.g. "this snapshot has the following limitations".)
    if expect_label != "PDPA MONITOR":
        assert "PDPA MONITOR" not in text


def test_compliance_score_table_stashes_overall_for_persistence():
    """`_compliance_score_table` must write the headline compliance score back
    into the scan_data dict so the PDPA fulfillment can persist it and the
    Compliance Evidence Cover Sheet can display the identical number (verbatim)
    instead of recomputing and drifting (53-vs-54 audit finding)."""
    from app.services.pdf_service import PDFService

    scan_data = {"some": "signals"}
    findings = [
        {"check_id": "no_consent_banner", "severity": "HIGH", "title": "No cookie banner"},
        {"check_id": "no_privacy_policy", "severity": "HIGH", "title": "No privacy policy"},
    ]
    PDFService()._compliance_score_table(findings, scan_data=scan_data)

    score = scan_data.get("computed_overall_compliance_score")
    assert isinstance(score, int) and 0 <= score <= 100, \
        f"overall compliance score not stashed for persistence: {score!r}"


def test_generate_pdf_stashes_score_on_top_level_for_flattened_scan_data():
    """The main scan path (process_report_task) flattens scan-evidence fields
    directly onto pdf_data (no nested 'scan_data' key). generate_pdf must then
    stash the dimension-weighted compliance score on the TOP-LEVEL pdf_data dict,
    because that's where the persist logic reads it back to store on the Report.

    This is the exact gap behind the cover-sheet 53-vs-54 divergence: the score
    was computed and printed (53) but never read back / persisted, so the cover
    sheet fell back to 100-risk (54). The score must also differ from a naive
    100-risk number so the regression is meaningful.
    """
    from app.services.pdf_service import PDFService

    pdf_data = {
        "framework": "pdpa_quick_scan",
        "company_name": "Crayon Singapore",
        "created_at": "2026-06-14T10:39:00Z",
        "status": "completed",
        "risk_score": 46,  # 100 - 46 = 54, the divergent fallback
        "structured_report": {
            "detailed_findings": [
                {"check_id": "no_consent_banner", "severity": "HIGH", "title": "No cookie banner"},
                {"check_id": "missing_security_headers", "severity": "HIGH", "title": "Security headers missing"},
                {"check_id": "dpo_not_disclosed", "severity": "MEDIUM", "title": "DPO contact not disclosed"},
            ],
        },
        # Flattened scan evidence (as process_report_task passes it):
        "trackers": {"inventory": ["Google Ads", "Google Tag Manager", "LinkedIn Insight Tag"]},
        "ssl_grade": {"grade": "A"},
    }
    PDFService().generate_pdf(pdf_data)

    score = pdf_data.get("computed_overall_compliance_score")
    # Present on the TOP level (where the persist reads it) and a valid score.
    assert isinstance(score, int) and 0 <= score <= 100, \
        f"dimension-weighted score not stashed on top-level pdf_data: {score!r}"


@freeze_time("2026-05-24T12:00:00Z")
def test_pdpa_pdf_is_deterministic_when_time_frozen():
    """Two generations with identical input + frozen clock should match by length
    (full byte equality is fragile across reportlab font caches, but length is
    a useful regression guardrail)."""
    from app.services.pdf_service import PDFService

    data = {
        "framework": "pdpa_quick_scan",
        "company_name": "Deterministic Co",
        "created_at": "2026-05-24T12:00:00Z",
        "risk_score": 10,
        "findings": [],
    }
    a = PDFService().generate_pdf(data)
    b = PDFService().generate_pdf(data)
    assert abs(len(a) - len(b)) < 50, "PDF output drift > 50 bytes — investigate non-determinism"


@freeze_time("2026-05-24T12:00:00Z")
def test_unassessed_dimensions_excluded_from_overall_not_fabricated():
    """Dimensions whose underlying check did not run (retention with no clause
    classifier, breach with no PDPC check, cross-border with no hosting check,
    tracker with no rendered scan) must render N/A and be EXCLUDED from the
    weighted overall — never a fabricated 70/75 folded into the headline number.

    With no findings, every dimension that *was* assessed is clean (>=90). If the
    four un-assessed dimensions were still scored 70/75 the overall would be
    dragged into the ~80s; excluding them keeps it >=90. That gap is the test."""
    from app.services.pdf_service import PDFService

    # scan_data deliberately omits policy_clauses / hosting / pdpc_enforcement /
    # trackers so those four dimensions hit the "did not run" branch → N/A.
    scan_data = {"company_name": "NA Test Co"}
    PDFService()._compliance_score_table([], scan_data=scan_data)
    overall = scan_data.get("computed_overall_compliance_score")
    assert isinstance(overall, int)
    assert overall >= 90, (
        f"overall={overall}: un-assessed dimensions appear to be dragging the "
        f"headline number — they must be excluded, not scored 70/75"
    )


def _cell_texts(table):
    """Flatten a reportlab Table's cell Paragraphs to their source text."""
    out = []
    for row in getattr(table, "_cellvalues", []):
        cells = []
        for cell in row:
            txt = getattr(cell, "text", None)
            cells.append(txt if txt is not None else str(cell))
        out.append(cells)
    return out


def test_unassessed_retention_renders_na_not_fabricated_score():
    """The retention row must show N/A (not a fabricated '70/100 Partial') when
    the policy clause classifier did not run. Asserts on the rendered table cells
    directly — robust to PDF pagination / text-extraction quirks."""
    from app.services.pdf_service import PDFService

    # No policy_clauses / hosting / pdpc_enforcement / trackers → those four
    # dimensions are un-assessed.
    table = PDFService()._compliance_score_table([], scan_data={"company_name": "NA Co"})
    rows = _cell_texts(table)

    retention_rows = [r for r in rows if r and "Retention Limitation" in r[0]]
    assert retention_rows, "retention dimension row not found"
    score_cell, status_cell = retention_rows[0][1], retention_rows[0][2]
    assert "N/A" in score_cell, f"retention score should be N/A, got {score_cell!r}"
    assert "70/100" not in score_cell, "fabricated 70/100 still rendered"
    assert "N/A" in status_cell, f"retention status should be N/A, got {status_cell!r}"


@freeze_time("2026-05-24T12:00:00Z")
def test_positive_verdicts_carry_a_provenance_qualifier():
    """Layer-3: a "Compliant" dimension is an inference from public disclosure,
    not an audit finding. The report must say so, or a positive verdict reads
    as an assurance we never performed."""
    from app.services.pdf_service import PDFService
    from pypdf import PdfReader

    pdf_bytes = PDFService().generate_pdf({
        "framework": "pdpa_quick_scan",
        "company_name": "Acme Test Co",
        "created_at": "2026-05-24T12:00:00Z",
        "risk_score": 12,
        "findings": [],
    })
    raw = "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf_bytes)).pages)
    text = " ".join(raw.split())

    assert "Basis: automated public-site scan on 2026-05-24" in text
    assert "not an audit of internal controls" in text


# ── Narrative must never contradict the score table ──────────────────────────

_CLEAN_PATH_PHRASES = (
    "no PDPA violations",
    "No remediation tasks required",
    "No compliance actions required",
    "clean compliance assessment",
)


def _flat_text(pdf_bytes):
    from pypdf import PdfReader
    raw = "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf_bytes)).pages)
    return " ".join(raw.split())


@freeze_time("2026-05-24T12:00:00Z")
def test_non_compliant_dimensions_never_render_clean_path_narrative():
    """Sections 3/5/7/9 used to be gated on `structured_report.detailed_findings`
    alone while section 4 scored from raw scan evidence. When the findings failed
    to plumb through (the _fulfill_pdpa rebuild read them from the wrong nesting
    level), the report asserted "found no PDPA violations" directly above a table
    reading Cookie Consent 8/100 Non-Compliant. The narrative must be gated on
    the same verdict the table prints."""
    from app.services.pdf_service import PDFService

    pdf_data = {
        "framework": "pdpa_quick_scan",
        "company_name": "Contradiction Test Pte Ltd",
        "created_at": "2026-05-24T12:00:00Z",
        "status": "completed",
        "risk_score": 34,
        # No structured_report at all — the exact shape that produced the bug.
        "consent_mechanism": {"has_cookie_banner": False},
        "trackers": {
            "inventory": [
                "Google Analytics", "Google Tag Manager", "LinkedIn Insight Tag",
                "Meta Pixel", "The Trade Desk",
            ],
        },
    }
    pdf_bytes = PDFService().generate_pdf(pdf_data)
    text = _flat_text(pdf_bytes)

    # Precondition: at least one dimension really did score Non-Compliant.
    assert "Non-Compliant" in text, "fixture no longer produces a failing dimension"

    for phrase in _CLEAN_PATH_PHRASES:
        assert phrase not in text, (
            f"clean-path narrative {phrase!r} rendered despite Non-Compliant "
            f"dimension scores — section 3/5/9 contradicts section 4"
        )
    # And the narrative must name what actually failed. Findings are now derived
    # from the same dimension scores that render §4, so §3/§5/§9 enumerate the
    # failing dimensions by name instead of printing the older generic
    # "N dimensions scored below Compliant" fallback paragraph.
    assert "Cookie Consent Mechanism" in text
    assert "Third-Party Tracker Inventory" in text


@freeze_time("2026-05-24T12:00:00Z")
def test_fully_compliant_scan_still_renders_strengths_block():
    """Inverse guard: the contradiction fix must not turn every report negative.
    A scan with no findings and no failing dimensions keeps the strengths block."""
    from app.services.pdf_service import PDFService

    pdf_bytes = PDFService().generate_pdf({
        "framework": "pdpa_quick_scan",
        "company_name": "Clean Co Pte Ltd",
        "created_at": "2026-05-24T12:00:00Z",
        "status": "completed",
        "risk_score": 5,
        "findings": [],
    })
    text = _flat_text(pdf_bytes)

    assert "no PDPA violations" in text
    assert "No remediation tasks required" in text


# ── Per-dimension coverage invariant ─────────────────────────────────────────
#
# `is_clean = not findings and not failing_dims` only catches the *all-or-
# nothing* contradiction. It does not catch a partial mismatch: with one
# unrelated finding present, `is_clean` is False, §3/§5/§9 render a confident
# one-item remediation list, and the other failing dimensions in §4 go
# unmentioned. That shape is worse than the netpoleons report, which at least
# looked suspicious. The invariant that actually holds the line is stronger:
#
#   every failing, assessed dimension must map to at least one finding.


def _uncovered_failing_dimensions(service, findings, computed):
    """Failing assessed dimensions that no finding in `findings` speaks to.

    Uses the same keyword lists `_compute_dimension_scores` uses to attribute a
    finding to a dimension, so this cannot drift from the scoring logic.
    """
    blobs = [
        " ".join([
            (f.get("check_id") or ""), (f.get("type") or ""), (f.get("title") or ""),
        ]).lower()
        for f in findings if isinstance(f, dict)
    ]
    uncovered = []
    for name, _score, status, _note in computed["dimensions"]:
        if name in computed["not_assessed"] or status not in service._FAILING_STATUSES:
            continue
        keywords = service._DIMENSION_FINDING_SPEC.get(name, ([], "", []))[0]
        if not any(k in blob for blob in blobs for k in keywords):
            uncovered.append(name)
    return uncovered


def test_every_scored_dimension_has_a_finding_spec():
    """A newly added dimension must not be able to score Non-Compliant with no
    possible corresponding finding — that is exactly how Cookie Consent /
    Trackers / Retention §25 / Cross-Border §26 became unreportable when the
    score table was upgraded to the Tier 1-5 evidence keys and
    `_detect_violations` was not."""
    from app.services.pdf_service import PDFService

    service = PDFService()
    scored = {d[0] for d in service._compute_dimension_scores([], {})["dimensions"]}
    missing = sorted(scored - set(service._DIMENSION_FINDING_SPEC))
    assert not missing, (
        f"dimensions scored in Section 4 with no _DIMENSION_FINDING_SPEC entry: "
        f"{missing} — these can fail the table with no finding to report them"
    )


# (label, scan evidence, pre-existing findings)
_COVERAGE_SCENARIOS = [
    (
        "no findings at all (netpoleons shape)",
        {
            "consent_mechanism": {"has_cookie_banner": False},
            "trackers": {"inventory": ["Google Analytics", "Meta Pixel", "LinkedIn Insight Tag"]},
        },
        [],
    ),
    (
        "one unrelated finding present (partial mismatch)",
        {
            "consent_mechanism": {"has_cookie_banner": False},
            "trackers": {"inventory": ["Google Analytics", "Meta Pixel", "The Trade Desk"]},
            "policy_clauses": {"found": [], "missing": ["retention", "transfer"]},
            "hosting": {"country": "US"},
        },
        [{"type": "marketing_violation", "title": "No DNC reference", "severity": "MEDIUM"}],
    ),
    (
        "empty scan evidence",
        {},
        [],
    ),
]


@pytest.mark.parametrize("label,scan,findings", _COVERAGE_SCENARIOS)
def test_no_failing_dimension_is_left_without_a_finding(label, scan, findings):
    from app.services.pdf_service import PDFService

    service = PDFService()
    computed = service._compute_dimension_scores(findings, scan)
    derived = service._derive_findings_from_dimensions(findings, computed)
    after = list(findings) + derived

    assert not _uncovered_failing_dimensions(service, after, computed), (
        f"[{label}] Section 4 shows failing dimensions that Sections 3/5/9 never "
        f"mention: {_uncovered_failing_dimensions(service, after, computed)}"
    )


def test_derivation_does_not_duplicate_an_existing_finding():
    """Real AI-authored findings must always win — deriving on top of them would
    print the same remediation task twice."""
    from app.services.pdf_service import PDFService

    service = PDFService()
    scan = {"consent_mechanism": {"has_cookie_banner": False}}
    findings = [{"type": "no_consent_banner", "title": "Trackers fire before consent"}]
    computed = service._compute_dimension_scores(findings, scan)

    derived = service._derive_findings_from_dimensions(findings, computed)
    assert "Cookie Consent Mechanism" not in [d["title"] for d in derived]


def test_derivation_is_silent_on_a_clean_scan():
    """The inverse of the coverage invariant: nothing may be synthesized when
    no assessed dimension is failing."""
    from app.services.pdf_service import PDFService

    service = PDFService()
    computed = service._compute_dimension_scores([], {})
    passing = {
        "dimensions": [d for d in computed["dimensions"] if d[2] not in PDFService._FAILING_STATUSES],
        "not_assessed": computed["not_assessed"],
    }
    assert service._derive_findings_from_dimensions([], passing) == []


@freeze_time("2026-05-24T12:00:00Z")
def test_partial_mismatch_narrative_names_every_failing_dimension():
    """End-to-end form of the invariant: render the partial-mismatch shape and
    confirm the narrative names each dimension the table marks as failing."""
    from app.services.pdf_service import PDFService

    _, scan, findings = _COVERAGE_SCENARIOS[1]
    service = PDFService()
    computed = service._compute_dimension_scores(findings, scan)
    failing = [
        d[0] for d in computed["dimensions"]
        if d[0] not in computed["not_assessed"] and d[2] in service._FAILING_STATUSES
    ]
    assert len(failing) > 1, "fixture no longer exercises a multi-dimension failure"

    full = _flat_text(service.generate_pdf({
        "framework": "pdpa_quick_scan",
        "company_name": "Partial Mismatch Pte Ltd",
        "created_at": "2026-05-24T12:00:00Z",
        "status": "completed",
        "risk_score": 40,
        "findings": findings,
        **scan,
    }))

    # Scope to Section 5 (the developer remediation list) — asserting against
    # the whole document is worthless here, because the Section 4 table names
    # every dimension itself and would satisfy the assertion even while the
    # narrative stays silent. Section 5 is what the buyer actually works from.
    start = full.index("5. DEVELOPER IMPLEMENTATION TASKS")
    tasks = full[start:full.index("6. ASSESSMENT CONDUCTED BY", start)]

    # Every gap the AI did not report must now carry its own task. (Dimensions
    # an AI finding already covers render under *that* finding's title, not the
    # dimension name — `test_no_failing_dimension_is_left_without_a_finding`
    # is what proves those are covered.)
    for f in service._derive_findings_from_dimensions(findings, computed):
        # Compare on the plain-language stem — the parenthetical statute refs
        # ("(PDPA §11/13)") do not survive PDF text extraction reliably.
        stem = f["title"].split(" (")[0]
        assert stem in tasks, (
            f"{f['title']!r} scored failing in Section 4 but generates no task "
            f"in Section 5"
        )

    # And the generic "no task list accompanied this scan" fallback must be gone
    # — that paragraph is what a buyer saw instead of actionable work.
    assert "No AI-generated task list accompanied this scan" not in tasks
