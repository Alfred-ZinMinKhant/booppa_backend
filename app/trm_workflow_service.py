"""
MAS TRM Workflow Service — V12
Initialises 13-domain controls for an organisation and runs AI gap analysis.
"""
import logging
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.core.models import TrmControl, MAS_TRM_DOMAINS

logger = logging.getLogger(__name__)

# Default control refs per domain (abbreviated MAS TRM 2021)
_DOMAIN_REFS = {
    "Technology Risk Governance": "TRM-1",
    "IT Project and Change Management": "TRM-2",
    "Technology Operations": "TRM-3",
    "IT Outsourcing and Vendor Management": "TRM-4",
    "Cyber Security": "TRM-5",
    "Data and Information Management": "TRM-6",
    "Customer Awareness and Education": "TRM-7",
    "Incident Management": "TRM-8",
    "IT Audit": "TRM-9",
    "Business Continuity and Disaster Recovery": "TRM-10",
    "Technology Testing": "TRM-11",
    "Cloud Computing": "TRM-12",
    "Authentication and Access Management": "TRM-13",
}

# Domains that map to a binding MAS statutory notice (FSMA, May 2024). Used to
# sharpen the gap-analysis prompt so narratives cite the actual standard rather
# than generic filler. Not exhaustive — only domains with a clear, testable
# statutory hook are included.
_DOMAIN_NOTICE_MAP = {
    "Cyber Security": (
        "Notice 655/FSM-N06: multi-factor authentication, rapid security patching, "
        "and privileged-account controls are mandatory, not best-practice suggestions."
    ),
    "Authentication and Access Management": (
        "Notice 655/FSM-N06: multi-factor authentication and privileged-access "
        "management controls are mandatory."
    ),
    "Incident Management": (
        "Notice 644/FSM-N05: major incidents must be notified to MAS within 1 hour "
        "of discovery."
    ),
    "Business Continuity and Disaster Recovery": (
        "Notice 644/FSM-N05: critical systems must recover within 4 hours, and the "
        "recovery plan must be regularly tested — an untested BCP/DR plan is treated "
        "by MAS as an aspiration, not a control."
    ),
}


def initialise_trm_controls(organisation_id: str, db: Session) -> list[TrmControl]:
    """Create one TrmControl row per MAS TRM domain for a new org."""
    controls = []
    for domain in MAS_TRM_DOMAINS:
        ctrl = TrmControl(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            domain=domain,
            control_ref=_DOMAIN_REFS.get(domain),
            status="not_started",
        )
        db.add(ctrl)
        controls.append(ctrl)
    db.commit()
    logger.info("Initialised %d TRM controls for org %s", len(controls), organisation_id)
    return controls


async def run_gap_analysis(control: TrmControl, context: str, db: Session) -> TrmControl:
    """
    Use DeepSeek to generate a gap analysis narrative for a single control.
    `context` is free-text the user provides (e.g. existing policy description).

    Raises HTTPException if the API key is missing so the caller surfaces a
    clear error instead of silently leaving the row blank.
    """
    import json
    from fastapi import HTTPException
    from app.core.config import settings
    from app.services.ai_provider import DeepSeekProvider

    if not settings.DEEPSEEK_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="AI gap analysis is unavailable — DEEPSEEK_API_KEY is not configured on the backend.",
        )

    provider = DeepSeekProvider(settings.DEEPSEEK_API_KEY)
    system = (
        "You are a MAS TRM compliance expert. Always respond with strict JSON only "
        "(no markdown fences, no commentary)."
    )
    notice = _DOMAIN_NOTICE_MAP.get(control.domain)
    notice_block = (
        f"\nThis domain maps to a binding MAS statutory requirement: {notice}\n"
        f"Your gap analysis MUST test the context against this specific standard "
        f"(cite the concrete threshold, e.g. the 1-hour/4-hour window or MFA "
        f"requirement, don't just say \"improve controls\").\n"
        if notice else ""
    )
    user_prompt = (
        f"Analyse the following organisation context against the MAS Technology Risk "
        f"Management domain: **{control.domain}** (ref: {control.control_ref}).\n"
        f"{notice_block}\n"
        f"Context provided:\n{context}\n\n"
        f"Identify gaps and provide a concise gap analysis (max 200 words). "
        f"For each gap identified, state what *tested* evidence (not merely a "
        f"written policy) would close it — e.g. a dated DR failover test result "
        f"rather than a DR plan document. Also classify risk rating as one of: "
        f"low, medium, high, critical, and set status to one of: "
        f"gap | in_progress | compliant.\n\n"
        f"Respond in JSON: {{\"gap_analysis\": \"...\", \"risk_rating\": \"...\", \"status\": \"...\"}}"
    )

    raw = await provider.call_chat([
        {"role": "system", "content": system},
        {"role": "user", "content": user_prompt},
    ])
    if not raw:
        raise HTTPException(
            status_code=502,
            detail="AI gap analysis failed: DeepSeek returned no content.",
        )

    cleaned = raw.strip().replace("```json", "").replace("```", "").strip()
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.error("Gap analysis: non-JSON from DeepSeek for %s: %s", control.domain, cleaned[:200])
        raise HTTPException(
            status_code=502,
            detail=f"AI gap analysis failed: model returned non-JSON ({e}).",
        )

    control.gap_analysis = result.get("gap_analysis", "")
    control.risk_rating = result.get("risk_rating", "medium")
    control.status = result.get("status", "gap")
    control.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(control)
    logger.info("Gap analysis complete for control %s (%s)", control.control_ref, control.domain)
    return control
