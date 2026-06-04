# PDPA Engine — Engineering & Operations Guide

Audience: backend / frontend engineers, compliance team, on-call.
Last updated: 2026-06-04.

This document covers everything we built in the PDPA-engine upgrade work:
the scanner architecture, the 11 dimensions, the AI-assisted classifiers,
the remediation tracking workflow, the precedents data, the report
rendering paths, and how to extend any of it.

If you only have five minutes, read sections 1 and 9.

---

## 1. Quick architecture map

```
Browser (booppa-nextjs)
  │
  │  HTTP
  ▼
FastAPI (booppa_backend/app/api)
  │
  │  enqueue
  ▼
Celery worker (booppa_backend/app/workers/tasks.py)
  │
  ├─ _scan_site_metadata(url, company_name)
  │     │
  │     ├─ headers + cookies + privacy-policy fetch (httpx)
  │     ├─ NRIC classifier  ──► services/nric_classifier.py + pdf_nric_scanner.py
  │     ├─ Policy classifier ──► services/policy_clause_classifier.py
  │     ├─ Evidence enricher ──► services/evidence_enricher.py
  │     │       (PDPC enforcement, hosting/SSL, ACRA)
  │     └─ _detect_cookie_banner (Playwright + tracker network capture)
  │
  ├─ _record_dimension_snapshots ──► pdpa_dimension_history table
  ├─ _confirm_remediations       ──► finding_remediations table
  │
  └─ PDF render (services/pdf_service.py) → S3
```

All AI calls go through `services/ai_provider.py` (DeepSeek). The classifiers
include redaction + heuristic fallbacks so the system degrades gracefully
when no API key is configured.

---

## 2. The 11 dimensions

The PDPA report and the web score table both show the same 11 dimensions,
derived from the same `assessment_data` keys. Each dimension is independently
scored and rolled up via weighted average (see `_DIMENSION_WEIGHTS` in
`services/pdf_service.py`).

| Dimension                            | Source                                                          | Weight |
|--------------------------------------|-----------------------------------------------------------------|:------:|
| Cookie Consent Mechanism             | `consent_mechanism` + `trackers.inventory` (behaviour-aware)    |   2    |
| Third-Party Tracker Inventory        | `trackers.inventory` (Playwright `page.on("request")`)          |   2    |
| Privacy Policy (PDPA §11/13)         | `policy_clauses` (clause classifier, 6 clauses)                 |   2    |
| Security HTTP Headers                | `security_headers` (HSTS / CSP / X-CTO / X-FO / RP / PP)        |   1    |
| Cookie Attributes                    | findings heuristic (Secure / HttpOnly / SameSite)               |   1    |
| DNC Registry Reference               | `dnc_mention.mentions_dnc`                                      |   1    |
| Data Subject Rights Mechanism        | `policy_clauses.items[data_subject_rights]` + findings          |   1    |
| NRIC Exposure                        | `nric.kind` (collection / leakage / policy_mention / none)      |   2    |
| Retention Limitation (§25)           | `policy_clauses.items[retention]`                               |   1    |
| Data Breach Notification (§26B-D)    | `pdpc_enforcement.found` (cross-ref against PDPC register)      |   2    |
| Cross-Border Transfer (§26)          | `hosting.inferred_region` (Singapore vs. overseas)              |   1    |

When you add a new dimension, you must update **three** places to keep the
PDF and web view consistent:

1. `services/pdf_service.py` — `_compliance_score_table` (scoring) and
   `_scope_of_assessment_table` (scope row).
2. `services/pdpa_dimension_snapshot.py` — `compute_dimension_snapshots`
   so drift detection and history both pick it up.
3. `app/pdpa/report/ReportClient.tsx` — `computeScores` and `SCOPE_ROWS`
   in the frontend.

---

## 3. AI-assisted classifiers

Two classifiers run per paid scan. Both share the same shape: harvest →
classify (LLM or heuristic) → summarise.

### 3.1 NRIC classifier (`services/nric_classifier.py`)

- `harvest_candidates(html, source_url)` — bounded ~80-char snippets around
  every NRIC label match, plus snippets around any string matching the
  Singapore NRIC format `[STFG]\d{7}[A-Z]`.
- `find_valid_nric_values(text)` — checksum-validated NRIC values (very low
  false-positive rate; rejects random matches).
- All real NRIC values are redacted to `[REDACTED-NRIC]` **before** any
  external API call.
- LLM classifies each snippet as one of: `collection` | `leakage` |
  `policy_mention` | `unrelated`.
- `summarise()` rolls per-snippet results into a dimension verdict.

### 3.2 Privacy-policy clause classifier (`services/policy_clause_classifier.py`)

- Six clauses checked: `purpose`, `withdrawal`, `dpo_contact`, `retention`,
  `third_party`, `data_subject_rights`.
- English path: `harvest_clause_snippets` extracts anchor-bounded snippets,
  then `classify_clauses` (LLM) decides whether each clause is genuinely
  fulfilled (templated language that mentions the word doesn't count).
- Multilingual path: `classify_clauses_multilingual` skips the anchor
  harvest and feeds the raw policy text directly to the LLM with a
  multilingual prompt. Triggered when `primary_language` ∈ {zh, ms, ta}.
- `summarise()` produces `{score, status, present_count, missing, items}`.

Both classifiers fall back to a conservative heuristic when no
`DEEPSEEK_API_KEY` is set. Dev environments without the key will produce
mostly-uncertain results — not a bug; we never fabricate signals.

### 3.3 Cost & rate-limiting

- ~2 LLM calls per scan (NRIC + policy clauses).
- ~$0.002–0.005 per scan at current DeepSeek pricing.
- No caching layer yet. If scan volume grows 10×+, add a Redis-backed cache
  keyed on `(content_hash, prompt_version)` to deduplicate repeated scans
  of the same content.

---

## 4. Headless browser & tracker capture

`_detect_cookie_banner(url)` in `tasks.py` launches Chromium via Playwright,
hooks `page.on("request")` before navigation, then waits for `networkidle`
+ 30s for splash screens. Every captured request URL is classified against
`_TRACKER_DOMAINS` (GA, Meta Pixel, Hotjar, Mixpanel, Segment, Adobe,
LinkedIn, TikTok, X, MS Clarity, Bing UET).

All captured trackers are considered **pre-consent** because the scanner
never clicks the banner. Result is stored as:

```python
page_result["trackers"] = {
    "pre_consent": [{"vendor": "...", "sample_url": "...", "count": N}, ...],
    "post_consent": [],
    "inventory": ["Google Analytics", "Meta Pixel", ...],
    "total_requests_captured": N,
}
```

### Adding a new tracker

Append a `(needle, vendor_label)` tuple to `_TRACKER_DOMAINS` in
`app/workers/tasks.py`. `needle` is a lowercase substring matched against
the full request URL; pick a stable string that doesn't co-occur with
unrelated domains (prefer a path segment over a TLD).

---

## 5. Screenshot capture

`services/screenshot_service.py` runs a 6-provider chain:

1. Playwright (local Chromium) — best quality
2. Browserless (self-hosted)
3. Microlink
4. Thum.io
5. mshots (WordPress)
6. Screenshot.guru

Every branch validates the response with **magic-byte sniffing** before
encoding (`looks_like_image`) — defends against providers that return
HTML error pages when they can't reach the target URL. Without this guard
we used to base64-encode HTML and render it inside `<img src=data:...>`
in the report viewer, producing the "unstyled marketing page in the
screenshot slot" bug.

### Tuning timeouts

- `_PLAYWRIGHT_SETTLE_MS` (3000ms) — wait after networkidle. Increase if
  scanned sites have long animated intros.
- `_PLAYWRIGHT_NAV_TIMEOUT_MS` (25000ms) — page-load cap.
- `_capture_screenshot_with_timeout(url, timeout=45)` in `tasks.py` — outer
  budget. **Must be ≥ `_PLAYWRIGHT_NAV_TIMEOUT_MS + _PLAYWRIGHT_SETTLE_MS
  + render slack`** or Playwright will always be killed and we'll fall
  through to lower-quality providers.

---

## 6. Drift detection & dimension history

`pdpa_dimension_history` table stores one row per dimension per completed
scan. The monthly `check_compliance_drift_task` (in `services/compliance_drift.py`)
compares the latest two snapshots per vendor:

- Overall risk-score delta ≥ 10% → existing `ComplianceDriftEvent` fires.
- **Per-dimension flip Compliant → Partial / Non-Compliant** → also fires
  a `ComplianceDriftEvent`, even when overall is steady (regressions on
  high-weight dimensions like NRIC can be silently masked by averaging).
- Per-dimension flips are attached to `details.dimension_flips` for the
  email template.

The monthly email now has two coloured blocks:
- Green "Improvements since your last scan" — confirmed remediations.
- Red "Dimensions that regressed" — per-dimension Compliant→Non-Compliant flips.

---

## 7. Remediation tracking

Three-table dance:

- **`finding_remediations`** — user-marked fixes
  (`finding_key`, `status`, `confirmation_status`, `marked_at`, `confirmed_at`).
- **`pdpa_dimension_history`** — see section 6.
- **Stable finding keys** in `services/finding_keys.py` —
  `extract_finding_keys(assessment_data)` derives `{nric:leakage,
  clause:retention, tracker:google_analytics, ...}` from a scan's
  assessment data.

### Flow

1. User clicks "I fixed this" in the report viewer.
2. `POST /api/remediations/reports/{report_id}` validates the key is
   actually present in this scan, then writes a row with
   `confirmation_status='pending'`.
3. On the next scan, `_confirm_remediations(db, report)` walks every
   pending/regressed remediation for the vendor and checks whether the
   finding key still appears in the current `extract_finding_keys`.
   - Gone → `confirmed` + timestamp + confirming_report_id.
   - Still present → `regressed`.

### Adding a new finding key

If you add a new dimension or detection (section 2 above), extend
`extract_finding_keys` in `services/finding_keys.py` so the key shows up
when the finding is present. Also add a human label to `_STATIC_LABELS` /
`_CLAUSE_LABELS` so the UI displays it nicely.

---

## 8. PDPC precedents

`services/pdpc_precedents.py` is a curated mapping of finding type to
public PDPC enforcement decisions. Each finding card in the report and
PDF shows a "Regulatory Precedent" line citing real cases with fines
when one is on file.

### How precedents are matched to findings

Backend (`pdf_service._finding_key_from`) and frontend (`precedentKeyFor`
in `ReportClient.tsx`) both derive a stable key from each finding:

- Anything with `nric` or `fin_number` in the type → `nric:collection`.
- Anything with `breach` or `notification` → `breach:pdpc_enforcement`.
- Everything else → `free:<slug>`.

The key looks up `PRECEDENTS[key]` and renders `precedent_summary(key)`.

### Adding new precedents (compliance-team workflow)

1. Open the PDPC decisions register:
   https://www.pdpc.gov.sg/all-commissions-decisions
2. For each decision you want to surface, capture:
   - Vendor name (as published).
   - Year of the decision.
   - Fine in SGD (0 if no financial penalty).
   - PDPA section breached (e.g. `§24 Protection Obligation`).
   - URL of the published decision.
   - One-sentence summary in your own words.
3. Pick the appropriate `finding_key`. Common keys:
   - `nric:collection` — unnecessary NRIC collection
   - `nric:leakage` — public NRIC exposure
   - `breach:pdpc_enforcement` — any §24/§26B breach
   - `clause:dpo_contact` — DPO not publicly disclosed
   - `tracker:google_analytics` — pre-consent GA firing
   - For the full key list, run `python -c "from app.services.pdpc_precedents
     import precedent_keys; print(precedent_keys())"`.
4. Add the entry to `PRECEDENTS` in `services/pdpc_precedents.py`.
5. Run `pytest tests/pdpa/test_pdpc_precedents.py` — the seed-data shape
   tests will catch missing keys / bad URLs / typos in year.

### Data quality rules (non-negotiable)

- Use only `pdpc.gov.sg` or `agc.gov.sg` URLs. The shape-test enforces this.
- Use the vendor name **as published in the decision**, not a marketing
  name or trade name.
- When in doubt, leave it out. A short curated list is far more defensible
  in front of a procurement officer than a long list with errors.

---

## 9. Migrations & deployment

Two migrations land with this work:

- `2026_06_01_0003-add_pdpa_dimension_history.py` — per-dimension history.
- `2026_06_01_0004-add_finding_remediations.py` — user-marked fixes.

Apply via the standard process: `alembic upgrade head`.

### Environment variables

- `DEEPSEEK_API_KEY` — required for the upgraded NRIC + policy classifiers.
  Without it, both fall back to heuristics and produce mostly-uncertain
  results for non-English content. CI / staging can run without; production
  must have it set.
- `BROWSERLESS_URL` — optional, falls back to local Playwright then public
  providers.
- `VIRUSTOTAL_API_KEY` — optional, used by `evidence_enricher`.

### Re-deploy checklist

1. `alembic upgrade head` against the target DB.
2. Confirm `DEEPSEEK_API_KEY` is present in worker env.
3. Confirm Playwright + Chromium are installed on the worker host
   (`playwright install --with-deps chromium`).
4. Smoke-test by enqueueing one scan and verifying:
   - PDF generates with all 11 dimensions.
   - `pdpa_dimension_history` gains 7+ rows for the new scan.
   - Screenshot embedded in the PDF is a real image (not HTML).

---

## 10. Test strategy

`tests/pdpa/` contains the engine-specific suite (96 tests as of writing).
Coverage tracks the contracts that matter:

- **`test_nric_classifier.py`** — checksum, redaction, harvest, heuristic
  classification, summary roll-up.
- **`test_policy_clause_classifier.py`** — English anchor harvest,
  withdrawal-regex regression, multilingual path (CN/MS without LLM).
- **`test_pdpa_dimension_snapshot.py`** — snapshot computation, diff
  surfaces only worsening transitions, improvements suppressed.
- **`test_finding_keys.py`** — extraction across all key categories,
  stability (same finding type → same key across scans).
- **`test_remediation_flow.py`** — confirmed/regressed transitions,
  idempotency, status filters.
- **`test_pdpc_precedents.py`** — lookup, summary formatting,
  seed-data shape (year, fine, URL validation).
- **`test_screenshot_validation.py`** — magic-byte image sniffer.
- **`test_pdf_dimensions_smoke.py`** — live PDF render asserting
  every new dimension + remediation section + precedent line surfaces
  in the rendered text.

Tests skip the heavy `tests/conftest.py` (which pulls Celery and FastAPI)
via `--noconftest`. Run with:

```
pytest tests/pdpa/ --noconftest
```

For CI, the standard `tests/conftest.py` is used and all PDPA tests
participate in the normal suite.

---

## 11. Known gaps & TODOs

- **PDPC precedents corpus** — seeded with 3 widely-reported cases
  (SingHealth, K Box). Compliance team to extend.
- **Multilingual classifier without LLM** — returns all-uncertain. Without
  a key set in staging/local, CN/MS/TA sites won't get a real assessment.
  Production worker must have `DEEPSEEK_API_KEY`.
- **Tracker post-consent capture** — schema reserves `post_consent` but
  it stays empty because the scanner never clicks the banner. Needs a
  banner-click heuristic before the bucket is populated.
- **Per-dimension drift backfill** — `pdpa_dimension_history` only fills
  going forward. Historic reports won't trigger per-dimension drift
  until they've been re-scanned twice under the new code.
- **No request-level caching of LLM calls** — fine at current volume,
  add Redis cache keyed on `(content_hash, prompt_version)` if it
  matters at scale.

---

## 12. File map (quick reference)

Backend
- `app/api/reports.py` — by-session response includes `scan_data` + `precedents`.
- `app/api/remediations.py` — remediation CRUD endpoints.
- `app/services/nric_classifier.py` — NRIC detection + redaction + checksum.
- `app/services/pdf_nric_scanner.py` — bounded linked-PDF crawler.
- `app/services/policy_clause_classifier.py` — clause classification (EN + multilingual).
- `app/services/evidence_enricher.py` — PDPC + ACRA + hosting + SSL signals.
- `app/services/pdpa_dimension_snapshot.py` — snapshot + diff helpers.
- `app/services/finding_keys.py` — stable finding-key derivation.
- `app/services/pdpc_precedents.py` — curated precedent data.
- `app/services/compliance_drift.py` — per-dimension drift + email payload.
- `app/services/pdf_service.py` — PDF generator (all dimensions + precedents + remediations).
- `app/services/screenshot_service.py` — multi-provider capture with magic-byte validation.
- `app/workers/tasks.py` — `_scan_site_metadata` + worker plumbing.
- `app/core/models_v8.py` — `PdpaDimensionHistory`, `FindingRemediation`, `ComplianceDriftEvent`.

Frontend
- `app/pdpa/report/ReportClient.tsx` — report viewer (score table, remediations, precedents).
- `app/vendor/remediations/page.tsx` — full remediation history page.
- `app/api/remediations/**/route.ts` — auth proxy to backend.
- `components/vendor/RemediationSummaryCard.tsx` — dashboard widget.
- `lib/remediations.ts` — client API wrapper.

Migrations
- `migrations/versions/2026_06_01_0003-add_pdpa_dimension_history.py`
- `migrations/versions/2026_06_01_0004-add_finding_remediations.py`

Tests
- `tests/pdpa/*` — 96 tests, see section 10.
