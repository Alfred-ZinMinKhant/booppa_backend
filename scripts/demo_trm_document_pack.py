#!/usr/bin/env python3
"""
scripts/demo_trm_document_pack.py — MAS TRM Document Pack acceptance matrix (shell front door)

Runs the acceptance test asked for verbatim: the full TRM Suite document-pack
fulfillment for a fresh bank-category customer and a fresh non-bank FI, plus the
no-licence-confirmed case every existing subscriber is currently in.

Thin wrapper over `app.services.trm_demo_harness.run_acceptance_matrix`, which
is the single code path shared with `POST /admin/trm/demo-document-pack`. It
drives the **real** Celery task body — only S3, the chain and email are stubbed.

`capture_pdf` is always on here: the pack email carries seven download links,
and a demo run that sends it is a demo run that mailed a customer.

Usage:
    python scripts/demo_trm_document_pack.py [output_dir]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.trm_demo_harness import run_acceptance_matrix  # noqa: E402

DEFAULT_OUT = Path("/tmp/trm_document_pack_demo")


def main():
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    out_dir.mkdir(parents=True, exist_ok=True)

    matrix = run_acceptance_matrix()

    for case in matrix["cases"]:
        case_dir = out_dir / f"case_{case['case']}_{case['mas_licence_type'] or 'unconfirmed'}"
        case_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n{'=' * 70}")
        print(f"Case {case['case']} — {case['company_name']}")
        print(f"  licence           : {case['mas_licence_type'] or '(not confirmed)'}")
        print(f"  outsourcing regime: {case['outsourcing_regime'] or '(none — register gated)'}")
        print(f"  pack status       : {case['status']}")
        print(f"  documents         : {len(case['delivered_doc_types'])}")
        print(f"  cost              : ${case['total_cost_usd'] or 0:.4f}")

        written = 0
        for doc_type, entry in sorted(case["documents"].items()):
            pdf = entry.get("pdf_bytes")
            if not pdf:
                reason = entry.get("skip_reason") or entry.get("error") or "not generated"
                print(f"    - {doc_type}: WITHHELD ({reason})")
                continue
            (case_dir / f"{doc_type}.pdf").write_bytes(pdf)
            written += 1
            print(f"    ✓ {doc_type}: {entry.get('word_count')} words  sha256={(entry.get('sha256') or '')[:16]}…")

        print(f"  written to        : {case_dir} ({written} PDFs)")
        for name, ok in case["checks"].items():
            print(f"  check {name:<16}: {'PASS' if ok else 'FAIL'}")

    print(f"\n{'=' * 70}")
    print(f"Acceptance matrix: {'PASS' if matrix['passed'] else 'FAIL'}")
    print("\nWhat to look for in the two register PDFs:")
    print("  Case A (bank) must cite MAS Notice 658 (and 1121 for merchant banks) and")
    print("  must NOT cite the Guidelines on Outsourcing. Case B (MPI) is the exact")
    print("  inverse. If both read the same, the branch is not working — that is the")
    print("  whole point of the test.")
    print("  The Cyber Hygiene Attestation must cite FSM-N06 by name, with Notice 655")
    print("  appearing only as superseded (cancelled 10 May 2024) — not a generic")
    print("  'cyber security' paragraph.")
    print("\nEvery document is an AI-generated DRAFT and carries that banner. None of")
    print("them is a statement of compliance until the entity reviews and signs it.")

    sys.exit(0 if matrix["passed"] else 1)


if __name__ == "__main__":
    main()
