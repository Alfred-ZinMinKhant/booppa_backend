#!/usr/bin/env python3
"""
scripts/demo_csp_onboarding.py — CSP onboarding output (shell front door)

Thin wrapper over `app.services.csp_demo_harness`, the single code path shared
with `POST /admin/csp/demo`. It walks a demo CSP org through onboarding and
writes three PDFs to the scratchpad: the Day-1 Registration Readiness Baseline,
the nominee director fit-and-proper assessment record, and the STR decision
record (a decision *not* to file, with its rationale) — exactly the output an
ACRA inspector would be handed.

`capture_pdfs=True` keeps the run offline: nothing is uploaded, nobody is mailed.

Usage:
    python scripts/demo_csp_onboarding.py [output_dir]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.csp_demo_harness import run_csp_onboarding_demo  # noqa: E402

DEFAULT_OUT = Path(
    "/private/tmp/claude-501/-Users-zinminkhant-Documents-Booppa-Booppa-booppa-backend/"
    "e64ea92b-4b08-4fd1-9136-7843bcb4be79/scratchpad"
)


def main():
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    out_dir.mkdir(parents=True, exist_ok=True)

    result = run_csp_onboarding_demo(capture_pdfs=True)

    entity = result["entity"]
    stub = result["profile_id"][:8]
    written = []
    for kind, fname in (
        ("baseline", f"csp_baseline_{stub}.pdf"),
        ("nominee", f"csp_nominee_fit_and_proper_{stub}.pdf"),
        ("str", f"csp_str_decision_{stub}.pdf"),
    ):
        pdf = result[kind].get("pdf_bytes")
        if not pdf:
            print(f"ERROR: {kind} PDF was not produced.", file=sys.stderr)
            sys.exit(1)
        path = out_dir / fname
        path.write_bytes(pdf)
        written.append(path)

    print(f"Entity: {entity['legal_name']} (UEN {entity['uen']})")
    print(f"Tenant: {result['user_email']}  profile: {result['profile_id']}")

    print("\n1. Day-1 Registration Readiness Baseline")
    print(f"   {written[0]}")

    n = result["nominee"]
    print("\n2. Nominee director fit-and-proper assessment")
    print(f"   Subject: {n['subject']}  →  {n['determination'].upper()}")
    print(f"   Outcome: {n['outcome']}")
    print(f"   {written[1]}")

    s = result["str"]
    print("\n3. STR decision record")
    print(f"   Client: {s['client']}  →  decision: {s['decision'].upper()}")
    print(f"   Rationale: {s['rationale'][:140]}...")
    print(f"   {written[2]}")

    print("\nThe fit-and-proper outcome and STR rationale are authored by the CSP, "
          "not generated — the platform captures, renders, and (in production) "
          "anchors them.")


if __name__ == "__main__":
    main()
