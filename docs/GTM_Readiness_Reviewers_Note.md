# Reviewer's Note — Booppa GTM Readiness Reports vs. Current Code

**Prepared for:** CTO & Engineering Leadership
**Re:** `Booppa_GTM_Readiness_Report.docx` (5 subscription tiers) and `Booppa_OneTime_GTM_Readiness_Report.docx` (5 one-time products)
**Date:** July 2026

---

## Read this first

Both GTM readiness reports are honest, evidence-based QA documents — but they were written against **delivered artifacts from an earlier build**. Every claim below was re-checked against the current codebase (file:line evidence given). The result:

- **Most of the severe findings — including both "P0 escalations" — are already fixed in the current code**, several with in-code comments that quote the reports' own artifacts (`suite-b.booppa.io`, "66 vs not available", "0 vs 2 open findings", "Security Review Log shown anchored, not delivered").
- **Several high-profile claims were never accurate** even against the artifacts (the "lifetime badge" contradiction, "no embeddable badge", "no DOCX", "court-admissible" language).
- **Only a small set of genuine gaps remained** — all now fixed in branch `fix/gtm-verification-open-gaps`.

**Recommendation:** do not action these reports as a live bug list. Use this note to close out already-resolved findings, and re-run the trial against the current build (in progress) for an accurate picture.

Status legend: **FIXED** (was real, now remediated) · **WRONG** (not accurate against code) · **OPEN→FIXED** (real gap this review found and fixed) · **DATA** (outcome of the test data, not a code defect).

---

## Subscription-tier report

| # | Finding | Status | Evidence / note |
|---|---|---|---|
| Defect A | Cross-document data inconsistency (cover vs source) | **FIXED** | Single-source resolvers `resolve_pdpa_score()` / `resolve_pdpa_findings()` — `app/services/pdpa_findings.py:11-95` |
| PDPA Monitor | Quick Scan 2 findings vs Monitor "Open findings: 0" | **FIXED** | Both read `booppa_report.detailed_findings` via `resolve_pdpa_findings` — `pdpa_findings.py:25-58`, `tasks.py:5005-5009` |
| Compliance Evidence | 66/100 vs "PDPA score not available" | **FIXED** | Both routed through `resolve_pdpa_score` — `pdpa_findings.py:61-95` |
| Compliance Evidence | Cover sheet "anchored" vs source "PENDING" | **FIXED** | Status derived from real tx-hash check; comment confirms it "was hardcoded ANCHORED here" — `evidence_pack/pdf_builder.py:368-411` |
| Defect C | "court-admissible" blockchain language | **WRONG** | No such string exists; the AI is explicitly instructed *not* to claim it — `booppa_ai_service.py:31`. Customer copy discloses testnet limits — `evidence_pack/document_generator.py:89-98` |
| Defect B | Win-probability static 33.6% / tender matching all WATCH / empty competitor intel | **DATA / NEEDS RE-TEST** | Model wiring exists; a single fabricated test vendor can produce uniform output. Re-verify with a real vendor before concluding a code defect. |

## One-time products report

| # | Finding | Status | Evidence / note |
|---|---|---|---|
| Issue A (P0) | Cross-tenant leak — `Vendor: Test` / `suite-b.booppa.io` bundled into a pack | **FIXED** | Was a deliverable-*selection* bug (blindly took latest Report row), not a scanner bug. Empty-score artifacts now skipped in two places + regression test — `tasks.py:2472-2519`, `stripe_webhook.py:2688-2718`, `tests/test_cover_sheet_waits_for_evidence_pack.py:137` |
| Issue B | ANCHORED next to PENDING; cover sheet lists undelivered doc | **FIXED** | Delivery-gated + real-tx-gated cover sheet — `tasks.py:2435-2467`; per-doc status tied to real tx — `pdf_builder.py:368-411` |
| Issue C (P0) | "admin-sim-" tx ID + "Court-admissible under Singapore Evidence Act" | **MOSTLY WRONG / gap fixed** | `admin-sim-` values are Stripe/session IDs, not chain hashes — `admin.py:795`. No "court-admissible" text in code. Residual gap (demo hash shown as tx) → **fixed**, see Open items below. |
| Issue D | RFP Complete: 0/15 VERIFIED answers | **DATA** | Verification IS wired to ACRA/SSL/GeBIZ/PDPC — `rfp_express_builder.py:110-126, 1541-1613`. A "Test" vendor with empty intake legitimately falls back to `ai_drafted`. Re-test with a real vendor. |
| Issue E | Vendor Proof "lifetime badge" vs 1-year expiry | **WRONG** | Pricing copy says **"Verified badge, renewed annually"** (`booppa-nextjs/app/pricing/page.tsx:295`); the 1-year certificate is consistent with it. The only "lifetime" copy is for the unrelated CSP pack. |
| Vendor Proof | Embeddable badge missing | **WRONG (PDF only)** | Full badge API exists — `app/api/widget.py` (`/badge/{id}.svg`, `/embed/{id}`); the activation email ships the snippet — `stripe_webhook.py:3139-3170`. It is simply not embedded inside the certificate PDF. |
| RFP Complete | No editable DOCX delivered | **WRONG** | DOCX is built for `rfp_complete` and for RFP-bearing bundles — `rfp_express_builder.py:258-265, 1821`; bundle component mapping — `stripe_webhook.py:1754-1874`. |
| Compliance Bundle | Only 5 of 7 governance docs | **OPEN→FIXED** | A completeness gate already errored+alerted+retried on <7 docs (`tasks.py:8237-8278`); added a per-doc in-place retry so transient AI failures self-heal without a full re-anchor. |

## Open items found by this review — now fixed (branch `fix/gtm-verification-open-gaps`)

1. **Demo/test-checkout hash could render as a "Transaction reference"** on the supplier due-diligence certificate. `demo_tx_hash()` is shape-valid but never mined; the renderer now requires `anchored AND is_real_onchain_tx` before printing a tx. Reachable only via admin simulate-purchase, but the email goes to a real address. — `supplier_due_diligence_generator.py:160-176`
2. **Quarterly PDPA snapshot rendered `anchor_tx` with no guard** (every other renderer had one). Now gated by `is_real_onchain_tx`. — `vendor_pdpa_snapshot_generator.py:237-243`
3. **Evidence-pack per-doc generation** now retries once in-place before deferring to the whole-pack completeness gate — reduces gas-costly full re-anchors on transient AI/JSON failures. — `evidence_pack/document_generator.py:760-782`
4. **web3.py 7.x broke every real (`demo=False`) anchor.** `SignedTransaction.rawTransaction` was renamed to `raw_transaction` (snake_case) in web3 ≥ 6; the code still used the removed camelCase name, so `send_raw_transaction` raised `AttributeError` on every anchor attempt. Added a version-tolerant `_raw_txn()` helper (prefers `raw_transaction`, falls back to `rawTransaction`). Discovered during the Phase-3 trial re-run. — `app/services/blockchain.py:10-19`, called at `:128` and `:165`

Regression tests: `tests/test_anchor_tx_render_guards.py` (5 tests, passing).

## Suggested next actions

1. Close out the FIXED findings in whatever tracker holds them — they will otherwise be re-triaged.
2. Treat the DATA-flagged items (win-probability variation, 0/15 VERIFIED) as **re-test tasks with a real vendor**, not code bugs.
3. Merge `fix/gtm-verification-open-gaps`.
4. Re-run the cloud-kinetics-style trial against the current build for an up-to-date artifact set.

*Verification method: both .docx reports extracted and cross-checked claim-by-claim against the working tree at `booppa_backend` via targeted code exploration. Not checked: the deployed/older revision — if a shipped build is still live, confirm it carries these fixes before relying on them.*
