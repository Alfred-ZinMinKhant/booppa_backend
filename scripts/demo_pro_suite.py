#!/usr/bin/env python3
"""
scripts/demo_pro_suite.py — Pro Suite feature proof (shell front door)

Thin wrapper over `app.services.pro_suite_demo_harness`, the single code path
shared with `POST /admin/pro-suite/demo`. It activates all four Pro-exclusive
capabilities on a demo tenant — two subsidiaries with different completion
profiles, customer branding, an active SAML config — regenerates the MAS TRM
baseline through the real worker, then runs a signed SAML assertion through the
real ACS route (plus a tampered one, which must be rejected).

Usage:
    python scripts/demo_pro_suite.py [output_dir]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.pro_suite_demo_harness import (  # noqa: E402
    activate_pro_features,
    run_sso_roundtrip,
)

DEFAULT_OUT = Path(
    "/private/tmp/claude-501/-Users-zinminkhant-Documents-Booppa-Booppa-booppa-backend/"
    "e64ea92b-4b08-4fd1-9136-7843bcb4be79/scratchpad"
)


def main():
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT

    # capture_pdf=True keeps the run offline: no S3 upload, no email to anyone.
    result = activate_pro_features(capture_pdf=True)

    pdf_bytes = result.get("pdf_bytes")
    if not pdf_bytes:
        print("ERROR: baseline task did not produce a PDF.", file=sys.stderr)
        sys.exit(1)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"pro_suite_demo_{result['org_id'][:8]}.pdf"
    out_path.write_bytes(pdf_bytes)

    print(f"Branded baseline written to: {out_path}")
    print(f"Tenant: {result['user_email']}  org: {result['org_slug']}")

    print("\n1. Multi-subsidiary")
    for sub in result["subsidiaries"]:
        print(f"   {sub['name']}: {sub['domains_complete']}/{sub['domains_total']} compliant, "
              f"open gap in {sub['open_gap_domain']}")
    print("   Group rollup: GET /vendor/trm/subsidiary-comparison")
    print("   Drill-down:   GET /vendor/trm/baseline/latest?subsidiary_id=<id>")

    wl = result["white_label"]
    print("\n2. White-label")
    print(f"   Header text: {wl['report_header_text']}")
    print(f"   Colours:     band {wl['secondary_color']}, accent {wl['primary_color']}")
    print(f"   Logo:        {'uploaded' if wl['logo_uploaded'] else 'SKIPPED (no S3 access offline)'}")

    print("\n3. SSO")
    rt = run_sso_roundtrip(user_id=result["user_id"])
    if rt["ok"]:
        print(f"   Signed assertion ACCEPTED at the real ACS route; session minted for {rt['name_id']}")
    else:
        print(f"   Round trip did not complete: {rt['error']}")
    bad = run_sso_roundtrip(user_id=result["user_id"], tamper=True)
    verdict = "REJECTED (correct)" if not bad["ok"] else "ACCEPTED — THIS IS A BUG"
    print(f"   Tampered assertion: {verdict}")

    print("\n4. Provisioning status (as printed in the baseline PDF)")
    for cap, status in result["provisioning_status"].items():
        print(f"   {cap}: {status}")

    if result.get("mock_idp_dir"):
        import shutil
        shutil.rmtree(result["mock_idp_dir"], ignore_errors=True)


if __name__ == "__main__":
    main()
