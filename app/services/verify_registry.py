from datetime import datetime
from typing import Any, Dict


def register_verification(
    assessment_data: Dict[str, Any] | None,
    evidence_hash: str,
    tx_hash: str | None,
    format_name: str = "BOOPPA-PROOF-SG",
    schema_version: str = "1.0",
) -> Dict[str, Any]:
    data = assessment_data if isinstance(assessment_data, dict) else {}

    payload = {
        "verify_id": evidence_hash,
        "tx_hash": tx_hash,
        "format": format_name,
        "schema_version": schema_version,
        "registered_at": datetime.utcnow().isoformat(),
    }

    registry = data.get("verification_registry")
    if not isinstance(registry, dict):
        registry = {}

    registry[evidence_hash] = payload

    return {
        "verification_registry": registry,
        "verify_id": evidence_hash,
        "verification_payload": payload,
    }
