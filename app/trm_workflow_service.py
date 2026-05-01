"""
MAS TRM Workflow Service — V12
Initialises 13-domain controls for an organisation and runs AI gap analysis.
"""
import logging
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.core.models_enterprise import TrmControl, MAS_TRM_DOMAINS

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
    Use Claude haiku-4-5 to generate a gap analysis narrative for a single control.
    `context` is free-text the user provides (e.g. existing policy description).
    """
    try:
        import anthropic
        client = anthropic.AsyncAnthropic()
        prompt = (
            f"You are a MAS TRM compliance expert. Analyse the following organisation context "
            f"against the MAS Technology Risk Management domain: **{control.domain}** "
            f"(ref: {control.control_ref}).\n\n"
            f"Context provided:\n{context}\n\n"
            f"Identify gaps and provide a concise gap analysis (max 200 words). "
            f"Also classify risk rating as one of: low, medium, high, critical."
            f"\n\nRespond in JSON: {{\"gap_analysis\": \"...\", \"risk_rating\": \"...\", \"status\": \"gap|in_progress|compliant\"}}"
        )
        message = await client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        import json
        raw = message.content[0].text.strip().replace("```json", "").replace("```", "")
        result = json.loads(raw)
        control.gap_analysis = result.get("gap_analysis", "")
        control.risk_rating = result.get("risk_rating", "medium")
        control.status = result.get("status", "gap")
        control.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(control)
        logger.info("Gap analysis complete for control %s (%s)", control.control_ref, control.domain)
    except Exception as e:
        logger.warning("Gap analysis failed for %s: %s", control.domain, e)
    return control
