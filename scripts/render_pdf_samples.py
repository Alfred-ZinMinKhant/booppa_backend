"""Render every migrated generator to /tmp/pdf_samples for visual review.

The point is pagination, so every fixture here is deliberately LONG — many
findings, many ROPA records, multi-page AI answers. Short fixtures never reach
the code paths that split tables, break sections or clip KeepTogether blocks,
which is exactly why the existing test suite (content assertions on small
inputs) did not catch the layout defects.

    python scripts/render_pdf_samples.py [name ...]
    pdftoppm -png -r 80 /tmp/pdf_samples/pdpa_long.pdf /tmp/pdf_samples/pdpa

Page counts are printed so before/after runs can be diffed: a count that drops
is usually a blank page removed; one that jumps means a CondPageBreak threshold
is set too high.
"""
from __future__ import annotations

import os
import sys

OUT_DIR = "/tmp/pdf_samples"

_LOREM = (
    "The organisation must document the lawful basis for collection, record the "
    "retention period against each purpose, and demonstrate that consent was "
    "obtained in a manner a reasonable person would consider appropriate. "
)


def _findings(n: int) -> list[dict]:
    sev = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
    return [
        {
            "title": f"Finding {i}: unconsented tracker fired before banner interaction",
            "type": f"tracker_violation_{i}",
            "check_id": f"CHK-{i:03d}",
            "severity": sev[i % 4],
            "description": _LOREM * (3 if i % 3 == 0 else 1),
            "details": _LOREM,
            "evidence": f"Network log entry {i} — analytics.js fired at t+120ms & before consent",
            "legislation_text": "PDPA 2012 s.13 — Consent Required; s.14 — Provision of Consent",
            "max_penalty": "Up to S$1,000,000",
            "remediation": _LOREM * 2,
            "acceptance_criteria": _LOREM,
            "deadline_short": "7 days",
            "penalty": {"amount": "S$50,000"},
        }
        for i in range(1, n + 1)
    ]


def _pdpa_long() -> bytes:
    from app.services.pdf_service import PDFService

    findings = _findings(14)
    return PDFService().generate_pdf({
        "report_id": "RPT-SAMPLE-0001",
        "company_name": "Ampersand & Sons <Holdings> Pte Ltd",  # exercises escaping
        "website_url": "https://example.test",
        "framework": "PDPA",
        "report_type": "pdpa_quick_scan",
        "created_at": "2026-07-01T10:00:00+00:00",
        "status": "completed",
        "audit_hash": "a" * 64,
        "tx_hash": "0x" + "b" * 64,
        "payment_confirmed": True,
        "verify_url": "https://booppa.io/verify/RPT-SAMPLE-0001",
        "structured_report": {
            "detailed_findings": findings,
            "recommendations": [
                {
                    "violation_type": f"issue_{i}",
                    "severity": "HIGH",
                    "actions": [f"Action {j}: {_LOREM}" for j in range(1, 5)],
                    "timeline": "14 days",
                }
                for i in range(1, 7)
            ],
            "legal_references": [
                {"title": "PDPA 2012 s.13 — Consent Required",
                 "url": "https://sso.agc.gov.sg/Act/PDPA2012#pr13-"},
            ] * 4,
        },
        "scan_data": {"company_name": "Ampersand & Sons <Holdings> Pte Ltd",
                      "findings": findings},
    })


def _pdpa_clean() -> bytes:
    """No findings — the short path that used to strand near-blank pages after
    each unconditional PageBreak."""
    from app.services.pdf_service import PDFService

    return PDFService().generate_pdf({
        "report_id": "RPT-SAMPLE-0002",
        "company_name": "Clean Co Pte Ltd",
        "website_url": "https://clean.test",
        "framework": "PDPA",
        "report_type": "pdpa_quick_scan",
        "created_at": "2026-07-01T10:00:00+00:00",
        "structured_report": {"detailed_findings": []},
        "scan_data": {"company_name": "Clean Co Pte Ltd", "findings": []},
    })


def _ropa_long() -> bytes:
    from app.services.ropa_generator import ROPA_INTAKE_SCHEMA, generate_ropa_lite_pdf

    rows = [
        {f["key"]: (_LOREM * 6 if i % 3 == 0 else f"Value for activity {i}")
         for f in ROPA_INTAKE_SCHEMA}
        for i in range(1, 10)
    ]
    return generate_ropa_lite_pdf(
        "Ampersand & Sons <Holdings> Pte Ltd", "201812345A", rows,
        dpo_name="Dana & Lee", dpo_email="dpo@example.test",
    )


def _csp_doc() -> bytes:
    from app.services.csp_doc_generator import generate_csp_document_pdf

    body = "\n".join(
        line
        for i in range(1, 9)
        for line in (f"## {i}. Control Section", _LOREM * 4, "- Bullet & item <one>", "")
    )
    pdf, _sha = generate_csp_document_pdf(
        "AML/CFT Policy", body,
        {"legal_name": "Ampersand & Sons Pte Ltd", "uen": "201812345A"},
    )
    return pdf


def _rfp_declaration() -> bytes:
    from app.services.rfp_declaration_generator import build_supplier_declaration_pdf

    out = build_supplier_declaration_pdf(
        "Ampersand & Sons Pte Ltd",
        intake={"company_name": "Ampersand & Sons Pte Ltd", "authorised_signatory": "Dana Lee"},
        acra_live={"found": True, "entity_status": "Live"},
        compliance_score=72,
    )
    # These builders swallow every exception and return b"" — a silent empty
    # deliverable. Surface it here rather than writing a 0-byte "sample".
    if not out:
        raise RuntimeError("builder returned empty bytes (exception swallowed; check logs)")
    return out


def _rfp_appendix_d() -> bytes:
    from app.services.rfp_appendix_d_generator import build_appendix_d_pdf

    qa = [
        {
            "question": f"D.{i} How do you handle & protect <personal data> in transit?",
            "answer": _LOREM * (8 if i % 3 == 0 else 1),
            "verification": {"source": "intake", "evidence": ["Intake form", "Website scan"]},
        }
        for i in range(1, 11)
    ]
    out = build_appendix_d_pdf("Ampersand & Sons Pte Ltd", qa_items=qa, compliance_score=72)
    if not out:
        raise RuntimeError("builder returned empty bytes (exception swallowed; check logs)")
    return out


SAMPLES = {
    "pdpa_long": _pdpa_long,
    "pdpa_clean": _pdpa_clean,
    "ropa_long": _ropa_long,
    "csp_doc": _csp_doc,
    "rfp_declaration": _rfp_declaration,
    "rfp_appendix_d": _rfp_appendix_d,
}


def main(names: list[str]) -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    from pypdf import PdfReader

    failures = 0
    for name in names or sorted(SAMPLES):
        path = os.path.join(OUT_DIR, f"{name}.pdf")
        try:
            data = SAMPLES[name]()
        except Exception as exc:  # noqa: BLE001 — a sample script; report and continue
            print(f"{name:<16} FAILED  {type(exc).__name__}: {exc}")
            failures += 1
            continue
        with open(path, "wb") as fh:
            fh.write(data)
        pages = len(PdfReader(path).pages)
        print(f"{name:<16} {pages:>3} pages  {len(data) // 1024:>5} KB  {path}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
