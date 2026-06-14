"""Central company constants for backend."""

COMPANY_NAME = "Booppa Smart Care LLC"
COMPANY_NAME_SINGAPORE = "Booppa Smart Care LLC (Singapore)"
# NOTE: Booppa has NO Singapore UEN. Never print a UEN for Booppa on any
# generated document — a fabricated regulatory identifier on a compliance
# artifact handed to procurers/regulators is a misrepresentation. (A prior
# hardcoded "202415732W" / "202506025W" was fictional.) If Booppa later obtains
# a real registration number, add it here and re-enable the disclaimer lines.
COMPANY_FRAMEWORK_VERSION = "BACF-v1.0"
COMPANY_LEGAL_FOOTER = "www.booppa.io  ·  Booppa Smart Care LLC"
COMPANY_DPO_EMAIL = "evidence@booppa.io"

__all__ = [
    "COMPANY_NAME", "COMPANY_NAME_SINGAPORE",
    "COMPANY_FRAMEWORK_VERSION", "COMPANY_LEGAL_FOOTER", "COMPANY_DPO_EMAIL",
]
