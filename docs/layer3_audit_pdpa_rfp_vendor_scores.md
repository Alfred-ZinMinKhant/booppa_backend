# Layer-3 Regulatory-Defensibility Audit — PDPA Reports, RFP Kits, Vendor Scores

**Date:** 2026-07-22 · **Trigger:** Gianpaolo's "broader ask" — apply the three-layer
commercially-ready lens (promise / customer need / **what the regulator actually requires**) as a
standing review, and re-check the products we called "working" (PDPA reports, RFP kits, vendor
scores) that passed layers 1–2 but were never rigorously checked against layer 3 the way the MAS
TRM baseline was.

**Layer-3 test applied to each product's *generated output*:**
1. Does it cite the actual binding external requirement by name where one exists?
2. Does it distinguish *tested / independently-verifiable* evidence from a bare written assertion
   or narrative? (MAS: "an untested plan is an aspiration, not a control.")

---

## 1. PDPA Reports — **Layer-3 clear on citation; caveat on scan-vs-tested**

- **Output:** findings in `app/services/pdpa_free_scan_service.py`; PDF in `app/services/pdf_service.py`;
  orchestrated by `app/workers/tasks.py::process_report_workflow`; resolved via `app/services/pdpa_findings.py::resolve_pdpa_findings`.
- **Citation — YES (strong).** Every finding carries a `legislation` field naming the section:
  s.24 Protection (`pdpa_free_scan_service.py:92`), s.13 Consent (`:208`), s.11 Openness (`:293`),
  s.11(3) DPO (`:317`), s.18 Purpose Limitation (`:165`). Rendered at `pdf_service.py:2302`.
  Breach-notification dimension names §26B-D (`pdf_service.py:1197`).
- **Tested-vs-asserted — PARTIAL (honest, but proxy-based).** The report has an explicit
  "Evidence & Verification" section (`pdf_service.py:646`) and grades three states instead of
  silently asserting: `Compliant` only when the PDPC check returned clean, `Non-Compliant` on an
  actual enforcement record, `Not Assessed` with "manual verification recommended" when the check
  is unavailable (`pdf_service.py:1197-1215`, same pattern at `:1098`, `:1241`). **Gap:** positive
  verdicts are inferred from a *website/HTML scan* (e.g. "consent mechanism detected"), and a
  breach-notification "Compliant" is a proxy from *absence of a PDPC enforcement record*
  (`pdf_service.py:1211`) — not proof of a tested breach-response procedure.
- **Verdict:** Layer-3 clear on citation and honest about un-assessed items. **Residual gap
  (low/medium):** a "Compliant" reads stronger than "we scanned your public site and saw no
  contra-indication." Consider a one-line provenance qualifier on positive verdicts ("basis:
  automated public-site scan on <date>; not an audit of internal controls").

## 2. RFP Kits — **Layer-3 clear**

- **Output:** `app/workers/tasks.py::fulfill_rfp_task` → `fulfill_rfp_package` →
  `app/services/rfp_express_builder.py`; enrichment from `app/services/evidence_enricher.py`
  (ACRA / PDPC / SSL Labs / VirusTotal / DNS / hosting), fetched at `rfp_express_builder.py:143-167`.
- **Grounded AND surfaced — YES.** `rfp_express_builder.py::_compute_verification` (`:1721`) builds a
  per-answer `{source, evidence[]}` provenance map with citable strings — "ACRA verified ·
  <registered_name>" (`:1782`), "SSL Labs grade <grade>" (`:1793`), "Privacy policy published at
  <url>" (`:1786`) — rendered under each answer in the PDF (`pdf_service.py:694-703`) plus a
  coverage line "Verified against ACRA, PDPC, SSL…" (`:319`).
- **Tested-vs-asserted — distinguished explicitly.** `source="ai_drafted"` → `confidence="generated"`
  (amber badge) vs verified → `"fact"` (`rfp_express_builder.py:466`, `:1738`); un-groundable claims
  get `[Verify: …]` markers and the template fallback carries "have not been independently verified"
  (`pdf_service.py:632-636`). ACRA name-vs-UEN discrepancies surface into the PDF (`:283`).
- **Verdict:** **Layer-3 clear.** This is the model the other two should follow.

## 3. Vendor Scores — **Layer-3 gap: provenance computed but not shown to buyer**

- **Output:** dimensions in `app/services/deep_scan_service.py` (`_pdpa_dimensions:81`,
  `_certifications_dimension:184`, `_financial_risk_dimension:211`; persisted `:347`); buyer-facing
  in `app/services/vendor_proof_generator.py` and `app/services/vendor_pro_report_generator.py`.
- **Citation — YES at compute layer.** Each dimension names its section — §20 (`:92`), §11(3) DPO
  (`:99`), §13/14 (`:107`), §21/22 (`:113`), §24 (`:138`), §25 (`:143`), §26 (`:150`), §26A-D (`:160`)
  — and stores a `detail` dict of the driving signal (`_dim`, `:49-58`).
- **Provenance shown to buyer — NO (the gap).** No API returns `dimension_name`/`detail`; the Vendor
  Pro report shows only the top-line number and dimension *names* in a drift table
  (`vendor_pro_report_generator.py:104,205`) and disclaims scores as "data-driven estimates"
  (`:235`). The checkable per-component source (which SSL grade, which site signal, which §) stays
  internal.
- **Tested-vs-asserted — the score is an inference, not tested.** Dimensions are website-mention
  heuristics (`:117` "heuristic: policy completeness proxy"; `:92` published = mentioned;
  `:160` breach grade from presence/absence of a PDPC record).
- **Note:** the tested-vs-documented primitive **already exists** at
  `vendor_features.py:1168 upload_trm_evidence` (`evidence_type ∈ {documented, tested}`,
  `tested_at`/`attestation`) — but it is scoped to the TRM workspace and **not wired into the
  Trust/Compliance vendor score**, which stays an opaque number to the buyer.
- **Bright spot:** the *identity* layer is well-sourced — Vendor Proof cert labels ACRA match_type
  (`vendor_proof_generator.py:141`), flags struck-off status (`:163`), and states it is "not, by
  itself, a compliance endorsement" (`:252`).
- **Verdict:** **Gap.** The score cites requirements and computes provenance but doesn't surface it,
  and each component is a public-signal proxy. This is the same class of issue the TRM baseline just
  fixed (opaque status → graded, sourced evidence).

---

## Prioritized remediation backlog (findings, not yet built — surfaced for triage)

| # | Product | Gap | Suggested fix | Priority |
|---|---|---|---|---|
| L3-1 | Vendor Scores | Per-component provenance (`detail` dict, § cited) computed but never shown to buyer; score is opaque | Add a "Score Basis" table to `vendor_pro_report_generator.py` rendering each dimension's driving signal + source, mirroring the RFP `{source, evidence[]}` pattern | **High** — 2.5x-price transparency, same class as TRM opacity |
| L3-2 | Vendor Scores | Score components are public-signal proxies, not tested; `documented\|tested` evidence primitive exists but isn't wired in | Let uploaded TRM tested-evidence lift/annotate the relevant score dimension so a buyer sees "tested" vs "inferred" | Medium |
| L3-3 | PDPA Reports | Positive verdicts read as audited compliance but are automated public-site scans | Add a one-line provenance qualifier on positive verdicts ("basis: automated public-site scan on <date>; not an audit of internal controls") | Low/Medium |
| — | RFP Kits | None — layer-3 clear | — | — |

**Recommendation:** RFP Kits are the reference implementation. Vendor Scores are the priority
(L3-1) — highest-price tier, same opacity problem the TRM baseline just solved, and the provenance
data already exists. PDPA Reports need only a framing qualifier, not new evidence plumbing.
</content>
