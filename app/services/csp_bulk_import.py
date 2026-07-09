"""
Booppa CSP Compliance Pack — Bulk Client Import Service
Allows CSPs with existing client lists to onboard in batch via CSV.

Reduces onboarding friction for CSPs with 10-100 existing clients.
CSV template downloadable from /api/v1/csp/clients/bulk-import/template

Supported formats:
  - CSV (comma-separated, UTF-8)
  - Excel (.xlsx) via openpyxl

Required columns: client_type, legal_name
Optional: uen_or_reg_no, country_of_inc, contact_name, contact_email,
          risk_rating, cdd_status, is_remote_onboarding,
          has_nominee_director, has_nominee_shareholder
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

ALLOWED_CLIENT_TYPES = {"individual", "company", "llp", "foreign_co"}
ALLOWED_RISK_RATINGS = {"low", "medium", "high", "very_high"}
ALLOWED_CDD_STATUSES = {"not_started", "in_progress", "completed", "expired", "failed"}

# Maximum rows per import
MAX_ROWS = 500


# ── RESULT TYPES ────────────────────────────────────────────────────────────

@dataclass
class ImportRow:
    row_number:    int
    raw_data:      Dict
    parsed_data:   Optional[Dict] = None
    errors:        List[str] = field(default_factory=list)
    warnings:      List[str] = field(default_factory=list)
    is_valid:      bool = True


@dataclass
class ImportResult:
    total_rows:      int
    valid_rows:      int
    invalid_rows:    int
    imported_count:  int
    skipped_count:   int
    errors:          List[Dict]   = field(default_factory=list)
    warnings:        List[Dict]   = field(default_factory=list)
    created_ids:     List[str]    = field(default_factory=list)
    import_id:       str          = ""
    completed_at:    str          = ""


# ── CSV TEMPLATE ──────────────────────────────────────────────────────────────

CSV_COLUMNS = [
    "client_type",          # REQUIRED: individual | company | llp | foreign_co
    "legal_name",           # REQUIRED: Full legal name
    "uen_or_reg_no",        # UEN (Singapore) or registration number (foreign)
    "country_of_inc",       # Country of incorporation (ISO alpha-2, e.g. SG, GB, CN)
    "contact_name",         # Primary contact person name
    "contact_email",        # Primary contact email
    "contact_phone",        # Primary contact phone
    "risk_rating",          # low | medium | high | very_high (default: medium)
    "cdd_status",           # not_started | in_progress | completed | expired (default: not_started)
    "services_provided",    # Comma-separated: company_formation,nominee_director,corp_secretarial
    "is_remote_onboarding", # TRUE | FALSE (default: FALSE)
    "has_nominee_director", # TRUE | FALSE (default: FALSE)
    "has_nominee_shareholder", # TRUE | FALSE (default: FALSE)
    "onboarded_date",       # YYYY-MM-DD format (when client relationship started)
    "notes",                # Internal notes (not stored in client record)
]

CSV_TEMPLATE_EXAMPLE_ROWS = [
    {
        "client_type": "company",
        "legal_name":  "Example Corp Pte Ltd",
        "uen_or_reg_no": "202312345A",
        "country_of_inc": "SG",
        "contact_name": "Jane Tan",
        "contact_email": "jane@example.com",
        "contact_phone": "+65 9123 4567",
        "risk_rating": "medium",
        "cdd_status": "completed",
        "services_provided": "company_formation,corp_secretarial",
        "is_remote_onboarding": "FALSE",
        "has_nominee_director": "FALSE",
        "has_nominee_shareholder": "FALSE",
        "onboarded_date": "2024-03-15",
        "notes": "Long-standing client, annual review due Q1 2025",
    },
    {
        "client_type": "individual",
        "legal_name":  "John Smith",
        "uen_or_reg_no": "",
        "country_of_inc": "GB",
        "contact_name":  "John Smith",
        "contact_email": "john@example.com",
        "contact_phone": "+44 7700 900123",
        "risk_rating":   "high",
        "cdd_status":    "completed",
        "services_provided": "nominee_director",
        "is_remote_onboarding": "TRUE",
        "has_nominee_director": "TRUE",
        "has_nominee_shareholder": "FALSE",
        "onboarded_date": "2023-11-01",
        "notes": "Foreign individual, EDD required",
    },
]


def generate_csv_template() -> bytes:
    """Generate a downloadable CSV template with headers and example rows."""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    writer.writerows(CSV_TEMPLATE_EXAMPLE_ROWS)
    return output.getvalue().encode("utf-8")


# ── PARSER ────────────────────────────────────────────────────────────────────

def _parse_bool(value: str, field_name: str) -> Tuple[bool, Optional[str]]:
    """Parse boolean from CSV string. Returns (value, error_or_none)."""
    if not value or value.strip() == "":
        return False, None
    v = value.strip().upper()
    if v in ("TRUE", "YES", "1", "Y"):
        return True, None
    if v in ("FALSE", "NO", "0", "N", ""):
        return False, None
    return False, f"Field '{field_name}' must be TRUE or FALSE, got: '{value}'"


def _parse_date(value: str, field_name: str) -> Tuple[Optional[datetime], Optional[str]]:
    """Parse date from YYYY-MM-DD string."""
    if not value or value.strip() == "":
        return None, None
    try:
        dt = datetime.strptime(value.strip(), "%Y-%m-%d")
        return dt.replace(tzinfo=timezone.utc), None
    except ValueError:
        return None, f"Field '{field_name}' must be YYYY-MM-DD format, got: '{value}'"


def _validate_row(row: Dict, row_number: int) -> ImportRow:
    """Validate a single CSV row. Returns ImportRow with errors if invalid."""
    import_row = ImportRow(row_number=row_number, raw_data=row.copy())
    errors   = []
    warnings = []
    parsed   = {}

    # ── client_type ──────────────────────────────────────────────────────
    client_type = row.get("client_type", "").strip().lower()
    if not client_type:
        errors.append("Column 'client_type' is required")
    elif client_type not in ALLOWED_CLIENT_TYPES:
        errors.append(
            f"Column 'client_type' must be one of: {', '.join(ALLOWED_CLIENT_TYPES)}. "
            f"Got: '{client_type}'"
        )
    else:
        parsed["client_type"] = client_type

    # ── legal_name ───────────────────────────────────────────────────────
    legal_name = row.get("legal_name", "").strip()
    if not legal_name:
        errors.append("Column 'legal_name' is required")
    elif len(legal_name) < 2:
        errors.append("Column 'legal_name' must be at least 2 characters")
    else:
        parsed["legal_name"] = legal_name

    # ── uen_or_reg_no ─────────────────────────────────────────────────────
    uen = row.get("uen_or_reg_no", "").strip()
    if uen:
        parsed["uen_or_reg_no"] = uen

    # ── country_of_inc ────────────────────────────────────────────────────
    country = row.get("country_of_inc", "").strip().upper()
    if country:
        if len(country) not in (2, 3):
            warnings.append(
                f"Column 'country_of_inc' should be ISO 2-letter code (e.g. SG, GB, US). "
                f"Got: '{country}'"
            )
        parsed["country_of_inc"] = country

    # ── contact info ──────────────────────────────────────────────────────
    if row.get("contact_name"):
        parsed["contact_name"] = row["contact_name"].strip()
    if row.get("contact_email"):
        email = row["contact_email"].strip()
        if "@" not in email:
            warnings.append(f"Column 'contact_email' does not look like a valid email: '{email}'")
        else:
            parsed["contact_email"] = email
    if row.get("contact_phone"):
        parsed["contact_phone"] = row["contact_phone"].strip()

    # ── risk_rating ───────────────────────────────────────────────────────
    risk = row.get("risk_rating", "medium").strip().lower()
    if risk and risk not in ALLOWED_RISK_RATINGS:
        warnings.append(
            f"Column 'risk_rating' must be one of: {', '.join(ALLOWED_RISK_RATINGS)}. "
            f"Defaulting to 'medium'. Got: '{risk}'"
        )
        risk = "medium"
    parsed["risk_rating"] = risk or "medium"

    # ── cdd_status ────────────────────────────────────────────────────────
    cdd = row.get("cdd_status", "not_started").strip().lower()
    if cdd and cdd not in ALLOWED_CDD_STATUSES:
        warnings.append(
            f"Column 'cdd_status' must be one of: {', '.join(ALLOWED_CDD_STATUSES)}. "
            f"Defaulting to 'not_started'. Got: '{cdd}'"
        )
        cdd = "not_started"
    parsed["cdd_status"] = cdd or "not_started"

    # ── services_provided ─────────────────────────────────────────────────
    services_raw = row.get("services_provided", "")
    if services_raw:
        services = [s.strip() for s in services_raw.split(",") if s.strip()]
        parsed["services_provided"] = services

    # ── booleans ──────────────────────────────────────────────────────────
    for bool_field in ("is_remote_onboarding", "has_nominee_director", "has_nominee_shareholder"):
        val, err = _parse_bool(row.get(bool_field, ""), bool_field)
        if err:
            warnings.append(err)
        parsed[bool_field] = val

    # ── onboarded_date ────────────────────────────────────────────────────
    date_val, date_err = _parse_date(row.get("onboarded_date", ""), "onboarded_date")
    if date_err:
        warnings.append(date_err)
    if date_val:
        parsed["onboarded_at"] = date_val

    import_row.parsed_data = parsed if not errors else None
    import_row.errors      = errors
    import_row.warnings    = warnings
    import_row.is_valid    = len(errors) == 0
    return import_row


# ── MAIN IMPORT FUNCTION ──────────────────────────────────────────────────────

def _cell_has_value(value) -> bool:
    """True if a DictReader cell holds non-blank content.

    csv.DictReader puts overflow fields (rows with more columns than headers)
    under the restkey as a *list*, so a naive ``v.strip()`` raises AttributeError
    on otherwise-empty rows. Handle list/None cells defensively.
    """
    if value is None:
        return False
    if isinstance(value, (list, tuple)):
        return any(_cell_has_value(v) for v in value)
    return bool(str(value).strip())


def parse_csv(content: bytes) -> Tuple[List[ImportRow], List[str]]:
    """
    Parse CSV bytes into validated ImportRow list.
    Returns (rows, file_level_errors).
    """
    file_errors = []

    try:
        text   = content.decode("utf-8-sig")   # utf-8-sig handles BOM from Excel
        reader = csv.DictReader(io.StringIO(text))
    except Exception as exc:
        return [], [f"Could not parse CSV file: {exc}"]

    # Validate headers
    if not reader.fieldnames:
        return [], ["CSV file appears to be empty"]

    missing_required = {"client_type", "legal_name"} - set(
        (reader.fieldnames or [])
    )
    if missing_required:
        file_errors.append(
            f"CSV missing required columns: {', '.join(missing_required)}. "
            f"Download the template from /api/v1/csp/clients/bulk-import/template"
        )
        return [], file_errors

    rows = []
    for row_num, row in enumerate(reader, start=2):   # start=2 because row 1 is headers
        if row_num - 1 > MAX_ROWS:
            file_errors.append(
                f"CSV exceeds maximum {MAX_ROWS} rows. "
                f"Split into multiple files."
            )
            break
        if not any(_cell_has_value(v) for v in row.values()):
            continue   # Skip empty rows
        rows.append(_validate_row(dict(row), row_num))

    return rows, file_errors


def parse_excel(content: bytes) -> Tuple[List[ImportRow], List[str]]:
    """Parse Excel (.xlsx) bytes into validated ImportRow list."""
    file_errors = []
    try:
        import openpyxl
    except ImportError:
        return [], ["openpyxl not installed — pip install openpyxl"]

    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        ws = wb.active
        rows_data = list(ws.iter_rows(values_only=True))
        if not rows_data:
            return [], ["Excel file appears to be empty"]

        headers = [str(h).strip() if h else "" for h in rows_data[0]]
        missing = {"client_type", "legal_name"} - set(headers)
        if missing:
            return [], [f"Excel missing required columns: {', '.join(missing)}"]

        rows = []
        for row_num, row_vals in enumerate(rows_data[1:], start=2):
            if row_num - 1 > MAX_ROWS:
                file_errors.append(f"Excel exceeds maximum {MAX_ROWS} rows")
                break
            row_dict = {headers[i]: (str(v).strip() if v is not None else "") for i, v in enumerate(row_vals)}
            if not any(row_dict.values()):
                continue
            rows.append(_validate_row(row_dict, row_num))

        return rows, file_errors
    except Exception as exc:
        return [], [f"Could not parse Excel file: {exc}"]


async def execute_import(
    rows:       List[ImportRow],
    csp_id:     str,
    db,
    auto_screen: bool = False,
) -> ImportResult:
    """
    Insert validated rows into the database.
    Optionally run sanctions screening on each client.

    Args:
        rows:         Validated ImportRow list from parse_csv/parse_excel
        csp_id:       UUID of the CspProfile
        db:           SQLAlchemy session
        auto_screen:  If True, run sanctions screening on each client name
    """
    import uuid as uuid_mod
    from app.core.models import CspClient

    valid_rows = [r for r in rows if r.is_valid]
    created_ids = []
    errors      = []
    warnings    = []

    for row in valid_rows:
        try:
            parsed = row.parsed_data or {}

            client = CspClient(
                csp_id=uuid_mod.UUID(csp_id),
                **{k: v for k, v in parsed.items() if hasattr(CspClient, k)},
            )

            # Sanctions screening
            if auto_screen and parsed.get("legal_name"):
                from .csp_sanctions import screen_entity, screen_individual
                screen_fn = (
                    screen_individual
                    if parsed.get("client_type") == "individual"
                    else screen_entity
                )
                result = screen_fn(parsed["legal_name"])
                if not result.is_clear:
                    client.risk_rating = "very_high"
                    warnings.append({
                        "row":     row.row_number,
                        "client":  parsed["legal_name"],
                        "message": (
                            f"Sanctions hit during import: {result.hit_count} match(es). "
                            f"EDD required. Client set to VERY_HIGH risk."
                        ),
                    })
                client.cdd_status = "not_started"   # Always require fresh CDD regardless

            db.add(client)
            db.flush()
            created_ids.append(str(client.id))

        except Exception as exc:
            logger.error("Import row %d failed: %s", row.row_number, exc)
            errors.append({
                "row":     row.row_number,
                "client":  row.raw_data.get("legal_name", "unknown"),
                "error":   str(exc),
            })

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Bulk import commit failed: %s", exc)
        raise

    # Collect row-level warnings
    for row in rows:
        for w in row.warnings:
            warnings.append({"row": row.row_number, "message": w})

    return ImportResult(
        total_rows     = len(rows),
        valid_rows     = len(valid_rows),
        invalid_rows   = len(rows) - len(valid_rows),
        imported_count = len(created_ids),
        skipped_count  = len(errors),
        errors         = [
            {"row": r.row_number, "client": r.raw_data.get("legal_name",""),
             "errors": r.errors}
            for r in rows if not r.is_valid
        ] + errors,
        warnings       = warnings,
        created_ids    = created_ids,
        completed_at   = datetime.now(timezone.utc).isoformat(),
    )
