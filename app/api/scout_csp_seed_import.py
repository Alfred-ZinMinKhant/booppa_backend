"""
scout_csp_seed_import.py
Booppa Smart Care LLC — SCOUT Agents, CSP seed-list ingestion

Reuses the exact pattern already proven in app/services/csp_bulk_import.py
(CSV upload, MAX_ROWS cap, per-row validation, a downloadable template).
"""

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File

from app.core.admin_auth import admin_auth

router = APIRouter(tags=["scout-agents"])

MAX_ROWS = 500
REQUIRED_COLUMNS = {"name"}
OPTIONAL_COLUMNS = {"uen", "licence_issue_date"}


@router.get("/seed-template.csv")
async def download_seed_template(_auth: bool = Depends(admin_auth)):
    """Downloadable starting point CSV template."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["name", "uen", "licence_issue_date"])
    writer.writerow(["Example CSP Services Pte Ltd", "202312345A", "2025-09-15"])
    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=scout_csp_seed_template.csv"},
    )


@router.post("/seed-upload")
async def upload_csp_seed_list(
    file: UploadFile = File(...),
    _auth: bool = Depends(admin_auth),
):
    """
    Parses and validates a CSV, then hands the seed list to
    scout_csp_scoring_task on heavy_queue. Scoring is network-bound
    (website discovery + AML scan per company), so it must not run
    inside the request — this returns as soon as the CSV is accepted
    and the results appear under /scout/pending when the task finishes.
    """
    if not file.filename.endswith((".csv",)):
        raise HTTPException(status_code=422, detail="Only .csv files are accepted")

    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail="File must be UTF-8 encoded")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not REQUIRED_COLUMNS.issubset(set(reader.fieldnames)):
        raise HTTPException(status_code=422, detail=f"CSV must include columns: {', '.join(REQUIRED_COLUMNS)}")

    rows = list(reader)
    if len(rows) > MAX_ROWS:
        raise HTTPException(status_code=422, detail=f"Maximum {MAX_ROWS} rows per upload, received {len(rows)}")

    seed_list = []
    skipped = 0
    for row in rows:
        name = (row.get("name") or "").strip()
        if not name:
            skipped += 1
            continue
        seed_list.append({
            "name": name,
            "uen": (row.get("uen") or "").strip(),
            "licence_issue_date": (row.get("licence_issue_date") or "").strip(),
        })

    if not seed_list:
        raise HTTPException(status_code=422, detail="No valid rows found (every row was missing 'name')")

    from app.workers.scout_celery_tasks import scout_csp_scoring_task

    async_result = scout_csp_scoring_task.apply_async(
        args=[seed_list], queue="heavy_queue"
    )

    return {
        "rows_received": len(rows),
        "rows_skipped": skipped,
        "queued": len(seed_list),
        "task_id": async_result.id,
        "message": "Seed list queued for scoring. Scored prospects appear under Pending when the run finishes.",
    }
