"""MAS licence classification for the regulated entity.

The licence class decides which *outsourcing* regime binds the customer, and
that is a hard fork, not a nuance:

  * Banks and merchant banks are bound by MAS Notices 658 / 1121 (binding since
    11 Dec 2024), which carry a MAS-published Outsourcing Register template.
  * Every other MAS-regulated FI follows the Guidelines on Outsourcing
    (Financial Institutions other than Banks) — guidance, different format.

Handing a bank a Guidelines-format register is not a slightly-worse document.
It is affirmative evidence, on the entity's own letterhead, that it has
misidentified its own regulatory regime.

There is deliberately **no "unknown" member** in MAS_LICENCE_TYPES. Unknown is
``None``. Encoding it as a value invites a ``.get(licence, DEFAULT)`` somewhere
downstream, which is exactly how a bank ends up holding an MPI register.
This also cannot be derived from the UEN — ACRA's open dataset carries no SSIC
or activity field (see the comment in app/api/stripe_checkout.py). It has to be
asked.
"""

from __future__ import annotations

from typing import Dict, Optional

# Canonical licence keys → human label shown in the UI and printed on documents.
MAS_LICENCE_TYPES: Dict[str, str] = {
    "bank": "Bank (MAS-licensed, Banking Act)",
    "merchant_bank": "Merchant Bank (Banking Act, Schedule)",
    "mpi": "Major Payment Institution (Payment Services Act 2019)",
    "spi": "Standard Payment Institution (Payment Services Act 2019)",
    "insurer": "Licensed Insurer (Insurance Act)",
    "capital_markets": "Capital Markets Services licensee (Securities and Futures Act)",
    "other_fi": "Other MAS-regulated financial institution",
}

# The two classes bound by Notices 658 / 1121 rather than the non-bank Guidelines.
BANK_CLASS = frozenset({"bank", "merchant_bank"})

# Regime identifiers used by the Outsourcing Risk Register generator to pick
# its prompt and JSON schema. The model is never shown both and asked to choose.
REGIME_BANK_NOTICES = "bank_notices"
REGIME_NON_BANK_GUIDELINES = "non_bank_guidelines"

# Tolerated inbound spellings from signup free text / Stripe metadata. Anything
# not listed normalises to None (unknown) rather than to a plausible guess.
_ALIASES: Dict[str, str] = {
    "bank": "bank",
    "banks": "bank",
    "full bank": "bank",
    "licensed bank": "bank",
    "wholesale bank": "bank",
    "digital full bank": "bank",
    "merchant bank": "merchant_bank",
    "merchant_bank": "merchant_bank",
    "mpi": "mpi",
    "major payment institution": "mpi",
    "major_payment_institution": "mpi",
    "spi": "spi",
    "standard payment institution": "spi",
    "standard_payment_institution": "spi",
    "insurer": "insurer",
    "insurance": "insurer",
    "licensed insurer": "insurer",
    "cms": "capital_markets",
    "capital markets": "capital_markets",
    "capital_markets": "capital_markets",
    "capital markets services": "capital_markets",
    "other": "other_fi",
    "other fi": "other_fi",
    "other_fi": "other_fi",
}


def normalise_licence(raw: Optional[str]) -> Optional[str]:
    """Map free-form input to a canonical key. Unrecognised → None (unknown)."""
    if not raw or not isinstance(raw, str):
        return None
    key = raw.strip().lower().replace("-", " ")
    key = " ".join(key.split())
    if key in MAS_LICENCE_TYPES:
        return key
    return _ALIASES.get(key) or _ALIASES.get(key.replace(" ", "_"))


def is_bank_class(licence: Optional[str]) -> bool:
    """True only for bank / merchant_bank. Unknown is False but callers must not
    rely on that — check ``licence is None`` first; see outsourcing_regime()."""
    return normalise_licence(licence) in BANK_CLASS


def licence_label(licence: Optional[str]) -> str:
    key = normalise_licence(licence)
    return MAS_LICENCE_TYPES.get(key, "Not confirmed") if key else "Not confirmed"


def outsourcing_regime(licence: Optional[str]) -> str:
    """Resolve the binding outsourcing regime.

    Raises on an unknown licence *by design*. There is no safe default: the
    caller must gate the Outsourcing Risk Register rather than pick one.
    """
    key = normalise_licence(licence)
    if key is None:
        raise ValueError(
            "MAS licence type is not set — the outsourcing regime cannot be "
            "resolved. Gate the Outsourcing Risk Register and ask the customer."
        )
    return REGIME_BANK_NOTICES if key in BANK_CLASS else REGIME_NON_BANK_GUIDELINES
