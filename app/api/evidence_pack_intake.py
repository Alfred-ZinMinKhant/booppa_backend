from app.core.route_classes import RetryAPIRoute
"""Evidence Pack intake endpoints for Compliance Evidence Pack buyers.

The `compliance_evidence_pack` SKU now produces the BCEP 7-document governance
pack, which needs a structured intake (org, sector, DPO, approver, systems, data
types, cross-border). Generation is deferred until the buyer submits that intake
— mirroring the RFP deferral pattern: at webhook time an EvidencePack row is
created with status='intake_pending' and the buyer is emailed a link here.

  GET  /evidence-pack-intake/pending        → buyer's outstanding intakes
  GET  /evidence-pack-intake/{id}           → one intake (status + prefill)
  POST /evidence-pack-intake/{id}/submit    → store intake, queue generation
"""
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.auth import verify_access_token
from app.core.db import get_db
from app.core.models import User
from app.core.models import EvidencePack

logger = logging.getLogger(__name__)

router = APIRouter(route_class=RetryAPIRoute)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)

# Fields the buyer must supply before the 7-document pack can be generated.
_REQUIRED_INTAKE = ["org_name", "uen", "sector", "employee_count", "approver_name", "approver_role"]
_REQUIRED_LISTS = ["data_types", "systems"]

# Singapore UEN formats (ACRA / data.gov.sg):
#   • Businesses (ROB):           8 digits + 1 letter      e.g. 52912345A
#   • Local companies (ROC):      9 digits + 1 letter      e.g. 201912345A
#   • Other entities (new UEN):   [TSR] + 2 digits + 2 letters + 4 digits + letter  e.g. T09LL0001B
# A BCEP document presented to a PDPC inspector with "UEN: Not provided" loses
# immediate credibility, so the gate rejects malformed/blank UENs at intake.
_UEN_PATTERNS = (
    re.compile(r"^\d{8}[A-Z]$"),
    re.compile(r"^\d{9}[A-Z]$"),
    re.compile(r"^[TSR]\d{2}[A-Z]{2}\d{4}[A-Z]$"),
)


def _normalise_uen(raw: str) -> str:
    return re.sub(r"\s+", "", str(raw or "")).upper()


def _is_valid_uen(uen: str) -> bool:
    return any(p.match(uen) for p in _UEN_PATTERNS)


def _resolve_user(token: str | None, db: Session) -> User:
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = verify_access_token(token)
    if not payload or not payload.get("sub"):
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.query(User).filter(User.email == payload.get("sub")).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.get("/pending")
def list_pending(
    token: str | None = Security(oauth2_scheme),
    db: Session = Depends(get_db),
):
    user = _resolve_user(token, db)
    rows = (
        db.query(EvidencePack)
        .filter(EvidencePack.user_id == user.id, EvidencePack.status == "intake_pending")
        .order_by(EvidencePack.created_at.desc())
        .all()
    )
    return {
        "items": [
            {
                "id": str(r.id),
                "pack_id": r.pack_id,
                "session_id": r.session_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


# Document-type → display title, mirrors DOC_META in
# app/services/evidence_pack/pdf_builder.py. Kept here so the workflow hub can
# render the 7-document list with stable ordering even before generation.
_DOC_ORDER = [
    ("dpmp", "Data Protection Management Programme"),
    ("ropa", "Record of Processing Activities (ROPA)"),
    ("data_inventory", "Data Inventory & Retention Schedule"),
    ("vendor_register", "Third-Party Processor Register & DPA Checklist"),
    ("breach_runbook", "Data Breach Response Runbook"),
    ("training", "Staff Training Register & Completion Evidence"),
    ("review_log", "Periodic Security Review Log"),
]


@router.get("/latest")
def latest_pack(
    token: str | None = Security(oauth2_scheme),
    db: Session = Depends(get_db),
):
    """The buyer's most recent Evidence Pack (any status) for the workflow hub.

    Powers the 7-document section on /compliance/cover-sheet so buyers can see
    the BCEP pack's progress (intake_pending → generating → ready) and download
    each document inline. Returns {pack: null} when the account has none.
    """
    user = _resolve_user(token, db)
    row = (
        db.query(EvidencePack)
        .filter(EvidencePack.user_id == user.id)
        .order_by(EvidencePack.created_at.desc())
        .first()
    )
    if not row:
        return {"pack": None, "documents": [d[1] for d in _DOC_ORDER]}
    urls = row.download_urls or {}
    anchoring = row.anchoring if isinstance(row.anchoring, dict) else {}

    def _anchored(dt: str) -> bool:
        a = anchoring.get(dt)
        return bool(isinstance(a, dict) and a.get("tx_hash"))

    documents = [
        {"doc_type": dt, "title": title, "download_url": urls.get(dt), "anchored": _anchored(dt)}
        for dt, title in _DOC_ORDER
    ]
    anchored_count = sum(1 for d in documents if d["anchored"])

    # Monthly tier recurring-value signals: when the pack regenerated, and when
    # the next anniversary-day refresh is due. anniversary_day is set for monthly
    # subscribers; one-time buyers have it null → next_refresh stays null.
    next_refresh = None
    anniv = getattr(user, "subscription_anniversary_day", None)
    if anniv:
        from datetime import date, timedelta
        today = date.today()
        day = max(1, min(28, int(anniv)))
        if today.day < day:
            nxt = today.replace(day=day)
        else:
            nxt = (today.replace(day=1) + timedelta(days=32)).replace(day=day)
        next_refresh = nxt.isoformat()

    return {
        "pack": {
            "id": str(row.id),
            "pack_id": row.pack_id,
            "status": row.status,
            "organisation": row.organisation,
            "session_id": row.session_id,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "anchored_count": anchored_count,
            "master_anchored": bool(isinstance(anchoring.get("master"), dict) and anchoring["master"].get("tx_hash")),
            "next_refresh": next_refresh,
            # Ordered list so the UI renders the 7 docs consistently whether or
            # not generation has produced download URLs yet.
            "documents": documents,
        }
    }


@router.get("/{pack_row_id}")
def get_intake(
    pack_row_id: str,
    token: str | None = Security(oauth2_scheme),
    db: Session = Depends(get_db),
):
    user = _resolve_user(token, db)
    row = (
        db.query(EvidencePack)
        .filter(EvidencePack.id == pack_row_id, EvidencePack.user_id == user.id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Evidence pack not found")
    return {
        "id": str(row.id),
        "pack_id": row.pack_id,
        "status": row.status,
        "session_id": row.session_id,
        # Prefill hints from the user profile so the form is fast to complete.
        "prefill": {
            # Prefill the resolved legal entity, not the raw signup string, so a
            # buyer who accepts the default doesn't stamp a bare domain on the pack.
            "org_name": (getattr(user, "legal_name", None) or getattr(user, "company", "") or ""),
            "uen": getattr(user, "uen", "") or "",
            "domain": getattr(user, "website", "") or "",
        },
        "download_urls": row.download_urls or {},
    }


@router.post("/{pack_row_id}/submit")
def submit_intake(
    pack_row_id: str,
    body: dict,
    token: str | None = Security(oauth2_scheme),
    db: Session = Depends(get_db),
):
    """Validate + store the structured intake, then queue pack generation.

    Required: org_name, uen, sector, employee_count, approver_name, approver_role,
    data_types (list), systems (list). Optional: dpo_name, dpo_email, cloud_provider,
    customer_types, other_markets, it_contact, domain.
    """
    user = _resolve_user(token, db)
    row = (
        db.query(EvidencePack)
        .filter(EvidencePack.id == pack_row_id, EvidencePack.user_id == user.id)
        .with_for_update()
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Evidence pack not found")
    if row.status not in ("intake_pending", "error"):
        raise HTTPException(status_code=409, detail="This evidence pack has already been submitted")

    intake = dict(body.get("intake") or body)
    # Fall back to the profile where the buyer left a field blank.
    intake.setdefault("org_name", (getattr(user, "company", "") or "").strip())
    intake.setdefault("uen", (getattr(user, "uen", "") or "").strip())
    intake.setdefault("domain", (getattr(user, "website", "") or "").strip())

    missing = [f for f in _REQUIRED_INTAKE if not str(intake.get(f) or "").strip()]
    missing += [f for f in _REQUIRED_LISTS if not isinstance(intake.get(f), list) or not intake.get(f)]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Missing required intake fields: {', '.join(missing)}",
        )

    # UEN gate — must be a well-formed Singapore UEN, not just non-empty.
    uen = _normalise_uen(intake.get("uen"))
    if not _is_valid_uen(uen):
        raise HTTPException(
            status_code=422,
            detail=(
                "UEN is invalid. Enter your Singapore Unique Entity Number exactly as it "
                "appears on your ACRA bizfile certificate (e.g. 201912345A or T09LL0001B). "
                "Find it at bizfile.gov.sg."
            ),
        )
    intake["uen"] = uen  # store the normalised value the documents will render

    row.intake = intake
    row.organisation = intake.get("org_name")
    row.status = "queued"
    db.commit()

    from app.workers.tasks import fulfill_evidence_pack_task
    fulfill_evidence_pack_task.delay(str(row.id))

    return {"status": "queued", "id": str(row.id), "session_id": row.session_id}
