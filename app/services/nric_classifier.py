"""
NRIC Classifier
===============
Classifies NRIC mentions found on a website into one of:

  - collection       : a form / input / instruction actually collecting an NRIC
  - leakage          : an NRIC value (or look-alike) exposed in public content
  - policy_mention   : the NRIC word appears in a privacy policy / advisory text
                       (e.g., "we do NOT collect NRIC") — not an exposure
  - unrelated        : false positive (e.g., "fin-tech", "Henrick", "national
                       registration of births" wording, etc.)

Classification is done with a short LLM call (DeepSeek) over a small set of
candidate snippets harvested from the HTML. The LLM only sees ~80-char windows
around each match, never raw NRIC numbers (those are redacted before the call).
"""
from __future__ import annotations


import json
import logging
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from app.services.ai_provider import DeepSeekProvider

logger = logging.getLogger(__name__)

# Singapore NRIC: [STFG]\d{7}[A-Z]. STFG = prefix indicating citizen/PR/foreigner.
NRIC_VALUE_RE = re.compile(r"\b([STFG])(\d{7})([A-Z])\b", re.IGNORECASE)
NRIC_LABEL_RE = re.compile(
    r"\b(nric|fin\s*number|fin\s*no\b|national\s+registration|identity\s*card\s*(?:no|number))\b",
    re.IGNORECASE,
)
SNIPPET_WINDOW = 80  # chars of context on each side of a match


@dataclass
class NricEvidence:
    kind: str          # collection | leakage | policy_mention | unrelated
    snippet: str       # redacted, sanitised context
    source_url: str    # page or document URL the snippet came from
    confidence: float  # 0..1 — model self-reported
    note: str = ""     # short rationale ("form field 'nric'", etc.)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "snippet": self.snippet,
            "source_url": self.source_url,
            "confidence": self.confidence,
            "note": self.note,
        }


def _redact_nric(text: str) -> str:
    """Replace any NRIC-looking value with '[REDACTED-NRIC]' so we never send
    real identifiers to the LLM or store them in evidence."""
    return NRIC_VALUE_RE.sub("[REDACTED-NRIC]", text)


def _windows(text: str, pattern: re.Pattern) -> Iterable[tuple[int, str]]:
    """Yield (offset, snippet) for each pattern hit with surrounding context."""
    for m in pattern.finditer(text):
        start = max(0, m.start() - SNIPPET_WINDOW)
        end = min(len(text), m.end() + SNIPPET_WINDOW)
        yield m.start(), text[start:end]


def harvest_candidates(html: str, source_url: str, max_candidates: int = 12) -> list[dict]:
    """Pull a bounded set of candidate snippets from HTML.

    Each candidate is {snippet, source_url, hint} where hint is 'label' (the
    word NRIC/FIN appears) or 'value' (an NRIC-shaped string appears).
    Snippets are redacted of real NRIC values before return.
    """
    if not html:
        return []

    candidates: list[dict] = []
    seen: set[str] = set()

    def _push(snippet: str, hint: str) -> None:
        cleaned = re.sub(r"\s+", " ", snippet).strip()
        cleaned = _redact_nric(cleaned)
        key = (hint, cleaned[:160])
        if cleaned and key not in seen and len(candidates) < max_candidates:
            seen.add(key)
            candidates.append({"snippet": cleaned, "source_url": source_url, "hint": hint})

    for _, snippet in _windows(html, NRIC_LABEL_RE):
        _push(snippet, "label")
    for _, snippet in _windows(html, NRIC_VALUE_RE):
        _push(snippet, "value")
    return candidates


def _is_valid_nric_checksum(nric: str) -> bool:
    """Singapore NRIC checksum validation. See PDPC NRIC Advisory & public
    PDPC test vectors. Returns False on any malformed input."""
    nric = nric.upper().strip()
    if len(nric) != 9 or nric[0] not in "STFG" or not nric[1:8].isdigit():
        return False
    weights = [2, 7, 6, 5, 4, 3, 2]
    digits = [int(c) for c in nric[1:8]]
    total = sum(d * w for d, w in zip(digits, weights))
    if nric[0] in "TG":
        total += 4
    st_checks = ["J", "Z", "I", "H", "G", "F", "E", "D", "C", "B", "A"]
    fg_checks = ["X", "W", "U", "T", "R", "Q", "P", "N", "M", "L", "K"]
    table = st_checks if nric[0] in "ST" else fg_checks
    return nric[8] == table[total % 11]


def find_valid_nric_values(text: str) -> list[str]:
    """Return NRIC-shaped strings that pass checksum validation. These are
    high-confidence leakage signals."""
    hits: list[str] = []
    for m in NRIC_VALUE_RE.finditer(text or ""):
        candidate = m.group(0).upper()
        if _is_valid_nric_checksum(candidate):
            hits.append(candidate)
    return hits


_CLASSIFIER_PROMPT = (
    "You are auditing a Singapore website for NRIC exposure under the PDPC NRIC "
    "Advisory (2018). For each snippet below, classify the NRIC reference into "
    "EXACTLY ONE of:\n"
    "  - collection: the page asks the user to provide their NRIC (form field, "
    "    instruction to enter, application form, etc.)\n"
    "  - leakage: a real NRIC value (or look-alike) appears publicly\n"
    "  - policy_mention: appears in privacy-policy / advisory / educational text "
    "    explaining the law or stating the org does NOT collect NRIC\n"
    "  - unrelated: false positive (e.g., 'fin-tech', 'henrick', registry-of-"
    "    births wording, regulatory references)\n\n"
    "Return ONLY a JSON array, one object per input snippet, in input order:\n"
    "  [{\"kind\": \"collection|leakage|policy_mention|unrelated\", "
    "\"confidence\": 0.0-1.0, \"note\": \"short reason\"}]\n"
    "No commentary, no prose."
)


async def classify_candidates(
    candidates: list[dict],
    provider: Optional[DeepSeekProvider] = None,
) -> list[NricEvidence]:
    """Send candidates to the LLM and return structured evidence.

    Falls back to heuristic classification when no provider / API key is
    configured, so the caller always gets a usable result.
    """
    if not candidates:
        return []

    if provider is None or not getattr(provider, "api_key", None):
        return [_heuristic_classify(c) for c in candidates]

    user_payload = {
        "snippets": [
            {"i": i, "hint": c["hint"], "text": c["snippet"]}
            for i, c in enumerate(candidates)
        ]
    }
    messages = [
        {"role": "system", "content": _CLASSIFIER_PROMPT},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]
    raw = await provider.call_chat(messages)
    parsed = _parse_classification(raw)

    if not parsed or len(parsed) != len(candidates):
        logger.warning(
            "NRIC classifier: model returned %s items, expected %s; falling back to heuristics",
            len(parsed) if parsed else 0, len(candidates),
        )
        return [_heuristic_classify(c) for c in candidates]

    out: list[NricEvidence] = []
    for c, p in zip(candidates, parsed):
        kind = p.get("kind") if isinstance(p, dict) else None
        if kind not in {"collection", "leakage", "policy_mention", "unrelated"}:
            kind = _heuristic_classify(c).kind
        confidence = float(p.get("confidence", 0.5)) if isinstance(p, dict) else 0.5
        confidence = max(0.0, min(1.0, confidence))
        note = (p.get("note") or "")[:200] if isinstance(p, dict) else ""
        out.append(NricEvidence(
            kind=kind,
            snippet=c["snippet"],
            source_url=c["source_url"],
            confidence=confidence,
            note=note,
        ))
    return out


def _parse_classification(raw: Optional[str]) -> Optional[list]:
    if not raw:
        return None
    raw = raw.strip()
    # Strip code fences if model wrapped output
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Try to recover an array slice
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, list) else None


def _heuristic_classify(c: dict) -> NricEvidence:
    """Conservative fallback used when no LLM is available.

    Rules of thumb:
    - hint=value (an NRIC-shaped string) → leakage
    - snippet contains form-collection cues → collection
    - snippet contains 'do not collect' / 'will not collect' / 'never collect'
      near NRIC → policy_mention
    - otherwise → policy_mention (safer than crying wolf)
    """
    snippet_lower = c["snippet"].lower()
    if c["hint"] == "value":
        kind = "leakage"
        note = "NRIC-shaped value detected without LLM classification"
    elif re.search(r"<(input|textarea|select)\b", snippet_lower) or re.search(
        r"\b(enter|provide|upload|submit|key in|type)\b.*\b(nric|fin)\b", snippet_lower
    ):
        kind = "collection"
        note = "Form input or collection verb near NRIC keyword"
    elif re.search(r"\b(do(es)? not|will not|never|no longer)\b[^.]{0,40}\b(collect|store|retain|use)\b", snippet_lower):
        kind = "policy_mention"
        note = "Negation phrasing near NRIC keyword"
    else:
        kind = "policy_mention"
        note = "Default heuristic — no collection cue detected"
    return NricEvidence(
        kind=kind,
        snippet=c["snippet"],
        source_url=c["source_url"],
        confidence=0.45,
        note=note,
    )


def summarise(evidences: list[NricEvidence]) -> dict:
    """Roll up per-evidence results into a single dimension summary that the
    PDF report and worker can persist."""
    if not evidences:
        return {
            "kind": "none",
            "status": "Compliant",
            "score": 100,
            "evidence_count": 0,
            "items": [],
        }
    has_leakage = any(e.kind == "leakage" for e in evidences)
    has_collection = any(e.kind == "collection" for e in evidences)
    only_mentions = all(e.kind in {"policy_mention", "unrelated"} for e in evidences)

    if has_leakage:
        kind, status, score = "leakage", "Non-Compliant", 0
    elif has_collection:
        kind, status, score = "collection", "Non-Compliant", 5
    elif only_mentions:
        # Mention in privacy policy is actually the *good* signal — keep score high
        kind, status, score = "policy_mention", "Compliant", 95
    else:
        kind, status, score = "unrelated", "Compliant", 100

    return {
        "kind": kind,
        "status": status,
        "score": score,
        "evidence_count": len(evidences),
        "items": [e.to_dict() for e in evidences[:8]],
    }
