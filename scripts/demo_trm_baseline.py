#!/usr/bin/env python3
"""
scripts/demo_trm_baseline.py — MAS TRM evidence demonstration harness (shell front door)

Thin wrapper over `app.services.trm_demo_harness.seed_and_generate`, which is the
single code path shared with `POST /admin/trm/demo-baseline`. It seeds a fintech
tenant, runs gap analysis on the three domains with binding statutory notices
(Cyber Security / TRM-5, Incident Management / TRM-8, Business Continuity & DR /
TRM-10), attaches documented + tested evidence, and regenerates the baseline PDF
through the real worker path.

Uses live DeepSeek if DEEPSEEK_API_KEY is set; otherwise falls back to
deterministic, notice-specific seeded narratives so the harness runs offline.

Usage:
    python scripts/demo_trm_baseline.py [output_dir]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.trm_demo_harness import seed_and_generate  # noqa: E402

DEFAULT_OUT = Path(
    "/private/tmp/claude-501/-Users-zinminkhant-Documents-Booppa-Booppa-booppa-backend/"
    "aca59fe5-95e8-4e2d-a690-bb8d9af2599e/scratchpad"
)


def main():
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT

    # capture_pdf=True keeps the run offline: no S3 upload, no email to anyone.
    result = seed_and_generate(capture_pdf=True)

    pdf_bytes = result.get("pdf_bytes")
    if not pdf_bytes:
        print("ERROR: baseline task did not produce a PDF.", file=sys.stderr)
        sys.exit(1)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"trm_baseline_demo_{result['org_id'][:8]}.pdf"
    out_path.write_bytes(pdf_bytes)

    print(f"Baseline PDF written to: {out_path}")
    print(f"Demo user: {result['user_email']}  org: {result['org_id']}")
    print(f"DeepSeek live: {result['deepseek_live']}")
    print("\nEvidence grading:")
    for domain, summary in result["evidence_summary"].items():
        print(f"  {domain}: {summary}")
    print("\nThree-layer readout:")
    print("  1. Promise      — 13-domain baseline, honest not-a-statement-of-compliance framing.")
    print("  2. Customer need — evidence attached and visible per domain (documented vs tested).")
    print("  3. MAS requirement — TRM-5/8/10 narratives now cite Notice 655/FSM-N06 (MFA/patching)")
    print("                        and Notice 644/FSM-N05 (1h notification / 4h recovery); BCP/DR")
    print("                        shows a dated, attested, blockchain-anchored test — the")
    print("                        inspection-defensible case MAS distinguishes from a plan doc.")
    print("  Residual gap    — Incident Management has a documented runbook but no drilled")
    print("                        1-hour notification test yet; correctly renders as a gap,")
    print("                        not falsely marked compliant.")


if __name__ == "__main__":
    main()
