"""
address_screen_detector.py — Component 6.2 (Address screening detector).

Detects virtual offices / serviced offices / shared addresses among vendors.
"""

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import List, Optional

# Known Singapore virtual-office / serviced-office / coworking providers.
PROVIDER_ADDRESS_SEEDS = [
    "regus", "wework", "justco", "the working capitol", "spacemob",
    "compass offices", "servcorp", "ucommune", "distrii", "found8",
]

FUZZY_MATCH_THRESHOLD = 0.85  # string-similarity threshold


def normalize_address(raw_address: str) -> str:
    """
    Normalize an address for comparison: lowercase, strip suite/unit/floor.
    """
    if not raw_address:
        return ""
    addr = raw_address.lower().strip()
    addr = re.sub(r'#\d+[-]?\d*', '', addr)
    addr = re.sub(r'\b(unit|suite|level|floor)\s*\d+\b', '', addr)
    addr = re.sub(r'\s+', ' ', addr).strip()
    return addr


@dataclass
class AddressSignal:
    vendor_name: str
    vendor_uen: str
    raw_address: str
    normalized_address: str = ""
    known_provider_match: Optional[str] = None
    shared_with_count: int = 0
    shared_address_signal: bool = False
    signal_basis: str = ""


def _check_known_provider(normalized_address: str) -> Optional[str]:
    for provider in PROVIDER_ADDRESS_SEEDS:
        if provider in normalized_address:
            return provider
    return None


def _fuzzy_equal(a: str, b: str) -> bool:
    return SequenceMatcher(None, a, b).ratio() >= FUZZY_MATCH_THRESHOLD


def screen_addresses(vendors: List[dict]) -> List[AddressSignal]:
    """
    vendors: list of dicts with at least 'vendor_name', 'uen', 'address'.
    Returns one AddressSignal per vendor.
    """
    signals = []
    for v in vendors:
        norm = normalize_address(v.get("address", ""))
        s = AddressSignal(
            vendor_name=v.get("vendor_name", ""),
            vendor_uen=v.get("uen", ""),
            raw_address=v.get("address", ""),
            normalized_address=norm,
        )
        s.known_provider_match = _check_known_provider(norm)
        signals.append(s)

    for i, s in enumerate(signals):
        if not s.normalized_address:
            continue
        count = 0
        for j, other in enumerate(signals):
            if i == j or not other.normalized_address:
                continue
            if s.normalized_address == other.normalized_address or _fuzzy_equal(s.normalized_address, other.normalized_address):
                count += 1
        s.shared_with_count = count

        reasons = []
        if s.known_provider_match:
            reasons.append(f"known virtual-office provider match: {s.known_provider_match}")
        if s.shared_with_count >= 2:
            reasons.append(f"address shared with {s.shared_with_count} other vendors in this batch")

        if reasons:
            s.shared_address_signal = True
            s.signal_basis = "; ".join(reasons)

    return signals


def from_vendor_db_rows(db_rows: List[dict]) -> List[AddressSignal]:
    """
    INTEGRATION POINT — CSP: CspClient.registered_address.
    Buyer: ManagedEntity.registered_address.
    """
    return screen_addresses(db_rows)
