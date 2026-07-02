"""Unit tests for the admin PDPA bulk-scan file parser.

Pure-function tests — no DB, Celery, or network. The endpoint/worker paths are
covered by the local e2e run (upload a small CSV against docker compose).
"""

import io

import pytest
from fastapi import HTTPException

from app.api.admin import MAX_BULK_SCAN_ROWS, _parse_bulk_scan_rows


def test_csv_happy_path_case_insensitive_headers_and_extra_columns():
    content = (
        b"Company_Name,Website_URL,notes\n"
        b"Acme,acme.example.com,ignored\n"
        b"Beta,https://beta.example.sg/,also ignored\n"
    )
    rows = _parse_bulk_scan_rows("companies.csv", content)
    assert rows == [
        {"company_name": "Acme", "website_url": "https://acme.example.com"},
        {"company_name": "Beta", "website_url": "https://beta.example.sg/"},
    ]


def test_csv_dedupes_urls_and_skips_blank_url_rows():
    content = (
        b"company_name,website_url\n"
        b"Acme,acme.example.com\n"
        b"Acme Again,ACME.example.com/\n"
        b"No Site,\n"
    )
    rows = _parse_bulk_scan_rows("companies.csv", content)
    assert len(rows) == 1
    assert rows[0]["company_name"] == "Acme"


def test_csv_missing_required_column_raises_400():
    with pytest.raises(HTTPException) as exc:
        _parse_bulk_scan_rows("bad.csv", b"name,url\nAcme,acme.com\n")
    assert exc.value.status_code == 400
    assert "missing required columns" in exc.value.detail


def test_csv_empty_file_raises_400():
    with pytest.raises(HTTPException) as exc:
        _parse_bulk_scan_rows("empty.csv", b"company_name,website_url\n")
    assert exc.value.status_code == 400


def test_csv_row_cap_enforced():
    lines = ["company_name,website_url"] + [
        f"Co {i},site{i}.example.com" for i in range(MAX_BULK_SCAN_ROWS + 1)
    ]
    with pytest.raises(HTTPException) as exc:
        _parse_bulk_scan_rows("big.csv", "\n".join(lines).encode())
    assert "maximum" in exc.value.detail


def test_csv_handles_excel_bom():
    content = "﻿company_name,website_url\nAcme,acme.example.com\n".encode("utf-8")
    rows = _parse_bulk_scan_rows("bom.csv", content)
    assert rows[0]["website_url"] == "https://acme.example.com"


def test_xlsx_happy_path():
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["company_name", "website_url"])
    ws.append(["Gamma", "gamma.example.org"])
    ws.append([None, None])  # blank row is skipped
    buf = io.BytesIO()
    wb.save(buf)
    rows = _parse_bulk_scan_rows("companies.xlsx", buf.getvalue())
    assert rows == [
        {"company_name": "Gamma", "website_url": "https://gamma.example.org"}
    ]


def test_company_name_falls_back_to_url():
    rows = _parse_bulk_scan_rows(
        "companies.csv", b"company_name,website_url\n,delta.example.com\n"
    )
    assert rows[0]["company_name"] == "https://delta.example.com"
