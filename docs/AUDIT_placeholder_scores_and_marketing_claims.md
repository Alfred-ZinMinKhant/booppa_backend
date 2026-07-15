# Cross-Product Audit — Placeholder Scores & Unwired Marketing Claims

**Date:** 2026-07-15
**Trigger:** PDPA Quick Scan review (ensigninfosecurity, Report ID
`ebae60e8-89ea-4273-861e-5148aa34a71c`) surfaced two defects that recur across
products round after round. Rather than fix only those instances, this pass sweeps
every product surface that prints a score/total or a marketing claim for the same
two structural patterns:

- **Pattern A** — a placeholder/default score assigned when the underlying check
  *did not run*, then silently folded into a customer-facing total as if measured.
- **Pattern B** — a marketing bullet that isn't wired end-to-end to real data.

The rule applied to every score surface: *"Is any input to this total a value
assigned when the underlying check did not run?"* and to every bullet: *"Can the
number/claim be traced to a real data source end-to-end?"*

---

## Findings

| # | Location | Pattern | Severity | Verdict / Action |
|---|----------|---------|----------|------------------|
| 1 | `app/services/pdf_service.py` — Retention (§25) `ret_score = 60/70` when clause classifier didn't run | A | **High** | **FIXED.** Un-assessed → `score=None`, status `N/A`, excluded from weighted overall + status logic; renders "N/A" not a number. |
| 2 | `pdf_service.py` — Data Breach Notification `breach_score = 70` when `pdpc_enforcement` not checked | A | **High** | **FIXED.** Same N/A treatment (was a fabricated "Partial" folded into overall). |
| 3 | `pdf_service.py` — Cross-Border Transfer `xbt_score = 75` when hosting not checked | A | **High** | **FIXED.** Same N/A treatment. |
| 4 | `pdf_service.py` — Tracker Inventory `tr_score = 70` when rendered scan didn't run | A | **High** | **FIXED.** Same N/A treatment. |
| 5 | `../booppa-nextjs` — "PDPC enforcement precedents per finding" (SGD 299) not wired; only `breach:pdpc_enforcement` populated | B | **High** | **FIXED.** New live per-obligation precedent index from the PDPC decisions register + honest "Regulatory basis" fallback now delivers the claim end-to-end. Per Alfred, the frontend bullet stays as-is (backend delivers it once the first weekly index job runs). |
| 6 | `app/services/csp_compliance_scorer.py:180` — CDD pillar `score = 70` when clients exist but none active | A | **Medium** | **FIXED.** Now `100`, consistent with the no-clients branch (score=100); the 70 was an arbitrary placeholder folded into the weighted CSP overall. |
| 7 | `pdf_service.py` — cookie/security-headers/DNC/rights/attributes `_has()==None → 94-97 "Compliant"` | A-ish | **Low** | **By design, documented.** The scan ran and detected no finding; "compliant" is a real (if optimistic) conclusion, not a fabricated measurement for an un-run check. Code comments added so it isn't "fixed" incorrectly. |
| 8 | `pdf_service.py:1229` — Cross-Border `xbt_score = 75` when hosting **checked** but provider not inferred | A-ish | **Low** | **Kept.** The check ran and was inconclusive — a real partial result, not "did not run". Left as Partial. |
| 9 | `app/services/pdpa_dimension_snapshot.py` | A | — | **Clean (reference impl).** Already skips un-assessable dimensions ("no false 'Compliant' rows"). The PDF path was the outlier and now matches it. |
| 10 | `app/api/managed_vendors.py:87-106` — vendor score 0/40/60/85 | A | — | **Clean.** Every branch is tied to real `report_count` / `notarized_count`; no un-run placeholder. |
| 11 | `app/services/cal.py:80-100` — `gap_score` heuristics (e.g. 60 "need notarization") | A | **Low** | **Kept.** Explicitly a *gap-to-next-level* progress heuristic (labelled as such), not a compliance measurement presented as fact. Not folded into a compliance total. |
| 12 | PDPA Monitor legislation-field mismatch (prior round) | B | — | **Regression-checked: holds.** No stale legislation field remains in `pdpa_monitor_delta_generator.py`. |
| 13 | Vendor Proof / ACRA lookup (prior round) | B | — | **Regression-checked: holds.** `evidence_enricher.fetch_acra_status` returns real fields; declaration generator tags VERIFIED vs CLIENT-DECLARED. |
| 14 | Win-probability (prior round) | B | — | **Regression-checked: holds.** `tender_service._compute_raw_probability` documents the removal of synthetic ±5% jitter; identical real signals score identically. |

---

## Round 2 — RFP / Cover Sheet / Vendor Trust Pack / ACRA (2026-07-15, verify pass)

Alfred flagged these across RFP Complete, Vendor Trust Pack, and the Compliance
Bundle, asking for confirmation they are *fixed **and** tested* — not merely
acknowledged. Each was traced to source; **all fixes are present**, and each now has
a named regression test.

| # | Item | Location | State / Test |
|---|------|----------|--------------|
| R1 | Privacy Policy scraped Google's URL (no same-domain check) | `rfp_express_builder._fetch_website_context._same_site` (line 560), applied at 596 | **FIXED + TESTED.** `tests/test_privacy_scraper_same_domain.py` drives the real scraper with mocked HTTP: off-domain (`policies.google.com`) link skipped, vendor-domain / relative link accepted. Source-level fix → covers RFP Complete, Vendor Trust Pack, Compliance Bundle equally. |
| R2 | "VERIFIED — BOOPPA" badge on an unfilled `[Verify:]` placeholder | `rfp_express_builder` qa_items gate (338–341): `source != "ai_drafted" AND not _PLACEHOLDER_RE.search(v)`; evidence line carried into Appendix D (`rfp_appendix_d_generator` 176–181) | **FIXED + TESTED.** `tests/test_rfp_builder_invariants.py` (placeholder never verifies even with an evidence source) + `tests/test_rfp_appendix_d.py` (VERIFIED item renders its evidence line; client-declared item renders none). |
| R3 | Cross-order file reuse (same `report_id` across two orders) | `rfp_express_builder:80` `report_id = uuid5(NAMESPACE_URL, f"rfp:{session_id}")` | **NOT A BUG — correct by design + TESTED.** Distinct Stripe orders carry distinct `session_id`s → distinct `report_id`s; result cache is `rfp_result:{session_id}`. Byte-identical kits only occur when the **same** `session_id` is reused (a test artifact). `tests/test_rfp_builder_invariants.py` locks distinct→distinct, same→identical, none→random. |
| R4 | Cover Sheet "Prepared for" leaked `evidence@booppa.io` | `cover_sheet_generator:539` reads `data.customer_email`; task threads real buyer email (`tasks.py:2794`, `helpers.py:512`) | **FIXED + TESTED.** `tests/test_cover_sheet_prepared_for.py` asserts buyer email present and `evidence@booppa.io` absent. |
| R5 | RFP Complete Kit row: blank SHA-256 in Anchored Documents | `tasks.py:2446` `file_hash = ad.get("file_hash") or r.audit_hash or "—"`; RFP report stores `file_hash`=evidence_hash (`rfp_express_builder:419`) | **FIXED + TESTED.** Same test file asserts the RFP row renders its truncated hash, not a dash. |
| R6 | Vendor Trust Pack price 249 < component 299 (self-cannibalising); dual price sources drift | `pricing.py:72` (349/34900) **and** `../booppa-nextjs/lib/pricing.ts:144` (349, badge `40% off`); "PDPA Snapshot" naming unified to "PDPA Quick Scan" | **FIXED (both sources in sync).** Backend (Stripe charge) and frontend (display + badge + "Save SGD 237") agree at SGD 349. No standalone `PDPA Snapshot` string remains. |
| R7 | ACRA auto-connect (real UEN/entity data, no mock) | Live: `evidence_enricher.fetch_acra_status` → `data.gov.sg/datastore_search` (`config.ACRA_DATASET_ID`). Offline seed: `acra_service.refresh_acra` → `discovered_vendors`; task `refresh_acra` (`tasks.py:6046`); beat `refresh-acra-monthly`; `scripts/sync_acra_now.py` | **IMPLEMENTED + TESTED (normalizer).** `tests/test_acra_service.py` pins the accept/skip policy and field mapping. Live path is network-gated — smoke via `python scripts/sync_acra_now.py --max 500`. |

**Round-2 tests:** `tests/test_privacy_scraper_same_domain.py`,
`tests/test_rfp_builder_invariants.py`, `tests/test_acra_service.py`,
`tests/test_cover_sheet_prepared_for.py`, and additions to
`tests/test_rfp_appendix_d.py`. Full affected set: **57 passed, 1 skipped**
(the 1 skip is the Stripe auto-skip). PDF-text assertions verified stable over 3 runs.

---

## Round 3 — deploy-time live pull + PDPC listing structure fix (2026-07-15)

Two follow-ups surfaced from prod worker logs while wiring the deploy-time pull:

| # | Item | Location | State / Test |
|---|------|----------|--------------|
| D1 | **Live pull on deploy.** The ACRA seed (`discovered_vendors`) and PDPC precedent index only populated on their monthly/weekly Beat ticks, so a fresh deploy could serve an empty registry-match table for weeks. | New `bootstrap_reference_data` task (`tasks.py`) wired to Celery's `worker_ready` signal (`celery_app.py`). Fires once per worker boot (= per deploy), self-gates on emptiness/staleness (`ACRA_SEED_STALE_DAYS=25`, PDPC index absent), Redis-debounced across replicas. | **IMPLEMENTED.** Enqueues `refresh_acra` / `build_pdpc_precedent_index` only when missing/stale, so routine redeploys don't re-pull the full register. |
| D2 | **PDPC precedent index built nothing** — `/all-enforcement-decisions` now 404s, and the live "All Commission's Decisions" page embeds decisions as a backslash-escaped **JSON island** (Angular app), not `<a>` tags, so the anchor-based scraper found 0 rows. This silently broke the "precedents per finding" claim (item #5 above) at the source. | `evidence_enricher.py`: `PDPC_ENFORCEMENT_URL` → `/all-commissions-decisions`; new `_extract_pdpc_decisions(html)` parses the JSON island (`\"label\"`/`\"url\"`) with an `<a>`-tag fallback; both `fetch_pdpc_enforcement` and `build_pdpc_precedent_index` use it. | **FIXED + TESTED.** Extracts **370** decisions from the live page (was 0). `tests/test_pdpc_precedents.py`: JSON-island parse + de-dupe, anchor-tag fallback, empty-on-unrelated-HTML. |
| D3 | **`VerifyRecord` level sync crashed** — `scoring.py` imported `compute_verification_depth` from `notarization_elevation` (which has no such symbol; it lives in `vendor_status`). | `scoring.py` import corrected to `app.services.vendor_status`. | **FIXED.** Verified `compute_verification_depth` imports and is callable. |
| D4 | **`email_suppressions` table missing in prod** (`relation ... does not exist`). Not a code bug — migration `2026_07_14_0001-add_email_suppressions` exists and is head; it simply hasn't been applied in prod yet. | `migrations/versions/2026_07_14_0001-…` | **PENDING DEPLOY.** Runs when the deploy's `alembic upgrade head` (one-off ECS migration task) executes. No code change. |

## Fixes applied in this pass

**Part 1 — un-assessed dimensions render N/A, excluded from the total**
(`pdf_service.py::_compliance_score_table`). Un-run checks (retention "classifier
did not run", breach not checked, cross-border hosting not checked, tracker
rendered-scan not run) are collected in a `_not_assessed_dims` set, given weight 0
so they are excluded from the weighted overall, and rendered as **N/A in both the
score and status cells** — no fabricated number is shown to the customer. The
persisted `computed_overall_compliance_score` (read by the Compliance Evidence
Cover Sheet) is now the weighted average over measured dimensions only.

**Part 2 — precedents populated from real live data**
(`evidence_enricher.py`, `pdpc_precedents.py`, `pdf_service.py`, `reports.py`,
`workers/tasks.py`, `workers/celery_app.py`). A weekly `build_pdpc_precedent_index`
Celery task scrapes the PDPC decisions register (reusing the existing list fetch),
classifies each decision into obligation categories from its formulaic title, and
best-effort parses fine/year from each decision page (null when unparseable — never
fabricated). `get_precedents` consults this live index (keyed by obligation) first,
then the human-verified static seed. Findings with no real decision on file show an
honest **"Regulatory basis"** (statute + guidance) line — never mislabelled a
precedent.

**Part 3 — CSP CDD pillar** (`csp_compliance_scorer.py`). Removed the arbitrary 70
for the no-active-clients edge; now 100, consistent with the no-clients branch.

## Follow-ups (logged, not fixed here)

- **First prod index run**: the "per finding" claim is fully backed once the
  weekly `build_pdpc_precedent_index` task has run in prod at least once. Frontend
  copy stays as-is (Alfred's call); confirm the index is populated after deploy.
- **Classifier/parse hardening**: the title→category rules and the fine/year parser
  are best-effort; the human-verified static seed + `verified` flag are the safety
  floor. Tighten as PDPC changes page markup.

## Standing check for new products

Before shipping any product that prints a score or a marketing bullet, answer:
1. Is any input to a customer-facing total a value assigned when the underlying
   check did not run? If so, mark it N/A and exclude it — never inject a number.
2. For every marketing bullet, can the number/claim be traced to a real data source
   end-to-end today? If not, either wire it or soften the copy before launch.
