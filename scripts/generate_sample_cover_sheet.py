"""Generate a sample Compliance Cover Sheet PDF for the pricing-page "See sample"
link.

One-off invocation. Writes the PDF into the Next.js `public/samples/` directory
so the link `/samples/compliance-cover-sheet-sample.pdf` resolves without any
backend round-trip. Re-run whenever `COVER_SHEET_SCHEMA_VERSION` bumps and the
sample drifts visibly from the production output.

Usage:
    python scripts/generate_sample_cover_sheet.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

# Make `app.*` imports resolve when running from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.cover_sheet_generator import generate_cover_sheet  # noqa: E402


def main() -> None:
    company = "Sample Pte Ltd"
    now = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")

    # 6 PDPA findings spanning severities — enough to show the buyer what
    # "Full Findings" actually looks like in a regulator-ready evidence pack.
    pdpa_findings = [
        {
            "title": "Privacy policy missing breach notification clause",
            "severity": "HIGH",
            "description": "Privacy policy does not specify the 72-hour PDPC notification timeline for notifiable data breaches.",
            "legislation_text": "PDPA s. 26A-26E — Data Breach Notification Obligation",
            "evidence": "Privacy page does not mention 'notifiable data breach' or 72-hour PDPC notification window.",
            "recommendation": "Add a Breach Notification section to the privacy policy stating the 72-hour PDPC notification commitment for notifiable breaches.",
        },
        {
            "title": "DPO contact published",
            "severity": "LOW",
            "description": "Dedicated DPO email on Privacy page satisfies PDPA accountability.",
            "legislation_text": "PDPA s. 11 — Accountability Obligation",
            "evidence": "Privacy page: dpo@example.com",
            "recommendation": "Add an SLA for DPO response time on the same page.",
        },
        {
            "title": "Cookie consent banner present but lacks granular toggles",
            "severity": "MEDIUM",
            "description": "Site shows a single accept/reject banner; PDPA-aligned but not granular for analytics vs. marketing cookies.",
            "legislation_text": "PDPA s. 13 — Consent Obligation",
            "evidence": "https://example.com/ — first-visit banner; one accept + one reject button.",
            "recommendation": "Add per-category toggles (Essential / Analytics / Marketing) and persist the buyer's choice for 365 days.",
        },
        {
            "title": "No published data retention schedule",
            "severity": "MEDIUM",
            "description": "Privacy policy mentions retention 'as long as necessary' without per-category timeframes.",
            "legislation_text": "PDPA s. 25 — Retention Limitation Obligation",
            "evidence": "Privacy page — 'Retention' section uses 'as long as necessary' without categorical limits.",
            "recommendation": "Publish a retention schedule by data category (e.g. account data: 7 years post-closure; marketing: 24 months from last engagement).",
        },
        {
            "title": "Subject Access Request (SAR) form provided",
            "severity": "LOW",
            "description": "SAR submission form available at /privacy/sar with email + identity verification.",
            "legislation_text": "PDPA s. 21 — Access & Correction Obligation",
            "evidence": "https://example.com/privacy/sar — form present, identity verification step described.",
            "recommendation": "Publish the 30-day SAR response SLA on the same page so requesters know the timeline.",
        },
        {
            "title": "No published list of overseas sub-processors",
            "severity": "HIGH",
            "description": "Privacy policy mentions 'cloud providers' generically without naming them; PDPA s. 26 requires comparable protection for overseas transfers.",
            "legislation_text": "PDPA s. 26 — Transfer Limitation Obligation",
            "evidence": "Privacy page — no Sub-processors / Third-party processors section enumerating named vendors.",
            "recommendation": "Publish a Sub-processors page listing named vendors, processing purpose, hosting region, and the safeguard relied on (SCCs / equivalent protection).",
        },
    ]

    # 15 RFP Q&A entries spanning PDPA, ISO, encryption, hosting, sub-processors,
    # breach history, DR/BCP — the actual rfp_complete questionnaire shape.
    qa_answers = [
        {
            "question": "Are personal data stored at rest using AES-256 or stronger encryption?",
            "answer": "Yes — at-rest encryption is enabled across all production databases using AWS KMS with AES-256 keys. Daily snapshots inherit the same key.",
            "confidence": "fact",
            "verification": {"source": "intake+website", "evidence": "Security page mentions AES-256 + KMS-managed keys."},
        },
        {
            "question": "What is the bidder's data residency for personal data in scope?",
            "answer": "Singapore residency — all production data is hosted in AWS ap-southeast-1 (Singapore region). No personal data is replicated outside the SG region.",
            "confidence": "fact",
            "verification": {"source": "intake", "evidence": "Confirmed via intake form: data_hosting=sg."},
        },
        {
            "question": "Is the bidder ISO/IEC 27001 certified?",
            "answer": "Yes — ISO/IEC 27001:2022 certified, certificate IS-7XX-22 issued by BSI, valid through 2027-04-30.",
            "confidence": "fact",
            "verification": {"source": "intake", "evidence": "Intake confirmed iso_status=certified, cert_number IS-7XX-22, expiry 2027-04-30."},
        },
        {
            "question": "Has the bidder appointed a Data Protection Officer (DPO)?",
            "answer": "Yes — a DPO is appointed and contactable at dpo@example.com. The DPO reports to the CEO and reviews policy changes quarterly.",
            "confidence": "fact",
            "verification": {"source": "intake+website", "evidence": "Privacy page lists DPO email; intake confirmed appointment."},
        },
        {
            "question": "Does the bidder maintain a Business Continuity / Disaster Recovery plan?",
            "answer": "Yes — RTO is 4 hours, RPO is 15 minutes. The plan is tested twice yearly with live failover to a secondary AZ in the same region.",
            "confidence": "fact",
            "verification": {"source": "intake", "evidence": "Intake confirmed BCP/DR tested semi-annually."},
        },
        {
            "question": "What encryption is used for data in transit?",
            "answer": "TLS 1.2 minimum across all external surfaces; internal service-to-service traffic uses mTLS within the VPC.",
            "confidence": "fact",
            "verification": {"source": "website", "evidence": "SSL Labs A+ rating on the main domain; TLS 1.2 minimum advertised."},
        },
        {
            "question": "Has the bidder experienced any notifiable data breaches in the past 24 months?",
            "answer": "No — no notifiable data breaches under PDPA s. 26A in the past 24 months.",
            "confidence": "fact",
            "verification": {"source": "intake", "evidence": "Intake confirmed breach_history=none."},
        },
        {
            "question": "What are the bidder's published sub-processors?",
            "answer": "AWS (hosting, Singapore region), Stripe (payments, US-hosted with SCCs), Resend (transactional email, EU-hosted with SCCs).",
            "confidence": "fact",
            "verification": {"source": "intake", "evidence": "Intake key_processors field listed AWS, Stripe, Resend."},
        },
        {
            "question": "How frequently is staff trained on PDPA / data-protection obligations?",
            "answer": "Annually for all staff, with a quarterly micro-module for staff handling personal data directly.",
            "confidence": "fact",
            "verification": {"source": "intake", "evidence": "Intake training_frequency=annual + quarterly micro-module."},
        },
        {
            "question": "What is the bidder's SAR (Subject Access Request) response SLA?",
            "answer": "30 days from receipt of a valid request, in line with PDPC guidance.",
            "confidence": "generated",
            "verification": {"source": "ai_drafted", "evidence": "No intake or website confirmation; drafted to PDPC default."},
        },
        {
            "question": "Does the bidder run regular penetration tests?",
            "answer": "Yes — third-party penetration test annually; remediation report shared with enterprise customers under NDA.",
            "confidence": "generated",
            "verification": {"source": "ai_drafted", "evidence": "Common industry posture; no explicit intake confirmation."},
        },
        {
            "question": "What's the bidder's policy on retention of personal data?",
            "answer": "Account data retained for 7 years after closure (tax / dispute window); marketing engagement data retained 24 months from last interaction; logs retained 90 days.",
            "confidence": "generated",
            "verification": {"source": "ai_drafted", "evidence": "Derived from PDPA s. 25 + typical enterprise schedule."},
        },
        {
            "question": "Does the bidder have a documented incident response plan?",
            "answer": "Yes — IR plan with on-call rotation, severity tiers, and a 72-hour PDPC notification track for notifiable breaches.",
            "confidence": "generated",
            "verification": {"source": "ai_drafted", "evidence": "Aligned with PDPA s. 26A-26E timeline."},
        },
        {
            "question": "Is the bidder a Singapore-registered entity?",
            "answer": f"Yes — {company}, UEN 202506025X, ACRA-registered.",
            "confidence": "fact",
            "verification": {"source": "external", "evidence": "ACRA registry lookup confirmed UEN."},
        },
        {
            "question": "What's the bidder's MAS TRM posture (if applicable)?",
            "answer": "Self-attested alignment with MAS TRM Guidelines (Jun-2021 revision) across the 13 domains; full gap analysis available under NDA.",
            "confidence": "generated",
            "verification": {"source": "ai_drafted", "evidence": "Self-attestation language; no MAS direct supervision."},
        },
    ]

    data = {
        "company_name": company,
        "customer_email": "sample@example.com",
        "report_id": "SAMPLE-0001",
        "bundle_type": "compliance_evidence_pack",
        # PDPA
        "pdpa_status": "completed",
        "pdpa_score": 76,
        "pdpa_details": {
            "framework": "PDPA",
            "completed_at": now,
            "website_url": "https://example.com",
            "risk_level": "medium",
            "total_findings": len(pdpa_findings),
            "severity_counts": {
                "High": sum(1 for f in pdpa_findings if f["severity"] == "HIGH"),
                "Medium": sum(1 for f in pdpa_findings if f["severity"] == "MEDIUM"),
                "Low": sum(1 for f in pdpa_findings if f["severity"] == "LOW"),
            },
            "detected_laws": ["PDPA (Singapore) 2012", "PDPA Amendment 2020"],
            "executive_summary": (
                "Automated scan of example.com surfaced 6 PDPA-relevant findings: "
                "two HIGH (missing breach-notification clause; no published sub-processor list), "
                "two MEDIUM (cookie consent granularity; retention schedule), "
                "and two LOW (DPO contact published; SAR form present). "
                "Remediation focuses on transparency obligations under s. 11, 13, 25, 26 and 26A."
            ),
            "findings": pdpa_findings,
            # v7: Scan Scope disclosure — auditor credibility signal.
            "scan_scope": {
                "pages_crawled": 14,
                "started_at": "2026-06-07T08:34:10Z",
                "completed_at": "2026-06-07T08:34:54Z",
                "ssl_grade": "A",
                "ssl_grade_checked_at": "2026-06-07",
                "excluded": [
                    "Authenticated areas (login-gated routes)",
                    "API endpoints",
                    "Mobile applications",
                    "Subdomains not crawled from the seed URL",
                ],
                "scanner_version": "Booppa PDPA Scanner v1",
            },
        },
        "pdpa_tx_hash": "0x" + "1a" * 32,
        # RFP
        "rfp_status": "completed",
        "rfp_details": {
            "product_type": "rfp_complete",
            "qa_count": len(qa_answers),
            "generated_at": now,
            "answer_source": "ai_grounded",
            "download_url": "https://example.com/sample-rfp.pdf",
            "executive_summary": (
                "RFP Complete Kit covers 15 PDPA, ISO, encryption, residency, "
                "sub-processor, BCP/DR, and breach-history questions. 11 of 15 "
                "answers verified against intake or website signals; 4 are "
                "AI-drafted from policy defaults and flagged for buyer review."
            ),
            "qa_answers": qa_answers,
        },
        "rfp_tx_hash": "0x" + "2b" * 32,
        # Combined Cover Sheet hash
        "tx_hash": "0x" + "3c" * 32,
        "network": "Polygon Amoy Testnet",
        # Cycle-scoped only (v6): PDPA report, RFP kit, Cover Sheet, signed CS.
        # No leakage of unrelated notarizations.
        "anchored_documents": [
            {
                "descriptor": "PDPA Quick Scan Report",
                "filename": "pdpa-report-sample.pdf",
                "file_hash": "1a" * 32,
                "tx_hash": "0x" + "1a" * 32,
            },
            {
                "descriptor": "RFP Complete Kit",
                "filename": "rfp-complete-sample.pdf",
                "file_hash": "2b" * 32,
                "tx_hash": "0x" + "2b" * 32,
            },
            {
                "descriptor": "Compliance Cover Sheet (this PDF)",
                "filename": "compliance-cover-sheet-sample.pdf",
                "file_hash": "3c" * 32,
                "tx_hash": "0x" + "3c" * 32,
            },
        ],
        "trm_domains": [],
        # v6: recommendations derived from PDPA findings — severity + SLA per
        # bullet so the buyer has a precise punch-list.
        "recommendations": [
            f["recommendation"] + f" ({f['legislation_text'].split(';')[0]} · {f['severity']} · remediate within 30 days)"
            for f in pdpa_findings if f["severity"] in {"HIGH", "CRITICAL"}
        ][:5] or None,
    }

    pdf_bytes = generate_cover_sheet(data)

    # Write to ../booppa-nextjs/public/samples/
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(here, ".."))
    nextjs_root = os.path.abspath(os.path.join(repo_root, "..", "booppa-nextjs"))
    samples_dir = os.path.join(nextjs_root, "public", "samples")
    os.makedirs(samples_dir, exist_ok=True)
    out_path = os.path.join(samples_dir, "compliance-cover-sheet-sample.pdf")
    with open(out_path, "wb") as f:
        f.write(pdf_bytes)
    print(f"Wrote {out_path} ({len(pdf_bytes)} bytes)")


if __name__ == "__main__":
    main()
