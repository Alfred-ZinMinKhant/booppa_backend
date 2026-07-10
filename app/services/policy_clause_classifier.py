"""
Privacy Policy §13 Clause Classifier
====================================
The PDPA 2012 s.11/13 Notification Obligation requires every privacy policy
to disclose specific items. This module checks whether a fetched policy
actually contains each required clause, rather than just confirming a policy
link exists.

Clauses checked:
  - purpose             : the purposes for which personal data is collected
  - withdrawal          : mechanism for withdrawing consent
  - dpo_contact         : Data Protection Officer name / email / channel
  - retention           : retention period or destruction/anonymisation policy
  - third_party         : disclosure to third parties / overseas transfers
  - data_subject_rights : access and correction rights (PDPA §21-22)

Architecture mirrors `app.services.nric_classifier`:
  1. harvest_clause_snippets(policy_html) → bounded list of candidate snippets
  2. classify_clauses(snippets, provider) → structured per-clause verdict
  3. summarise(...) → roll-up consumed by the PDF report
"""
from __future__ import annotations


import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from app.services.ai_provider import DeepSeekProvider

logger = logging.getLogger(__name__)

CLAUSES = ("purpose", "withdrawal", "dpo_contact", "retention", "third_party", "data_subject_rights")

# Heuristic anchors for each clause — also used by the fallback when no LLM
# is configured. The anchors are intentionally generous: the LLM disambiguates.
_CLAUSE_ANCHORS: dict[str, list[str]] = {
    "purpose": [
        r"\bpurpose[s]? (of|for which|of collection)\b",
        r"\bwhy (we|do we) collect\b",
        r"\bcollect.{0,40}\b(personal data|information)\b.{0,40}\b(for|to)\b",
    ],
    "withdrawal": [
        # "withdraw consent", "withdraw your consent", "withdrawal of consent",
        # "withdraw such consent", etc. — up to ~25 chars between verb and noun.
        r"\bwithdraw(al)?\b[^.\n]{0,25}\bconsent\b",
        r"\bopt[- ]?out\b",
        r"\b(revoke|cancel|rescind)\b[^.\n]{0,25}\bconsent\b",
        r"\bunsubscrib\w*",
    ],
    "dpo_contact": [
        r"\bdata protection officer\b",
        r"\bdpo\b",
        r"\bdpo@\w+",
        r"\bpdpa officer\b",
    ],
    "retention": [
        r"\bretention (period|policy)\b",
        r"\b(retain|store|keep).{0,40}\b(for|until|no longer than)\b",
        r"\b(delete|destroy|anonymis|anonymiz).{0,40}\b(when|after)\b",
        r"\bretention limitation\b",
    ],
    "third_party": [
        r"\bthird[- ]part(y|ies)\b",
        r"\bdisclos(e|ed|ure).{0,40}\b(to|with)\b",
        r"\bservice provider[s]?\b",
        r"\bsub[- ]processor[s]?\b",
        r"\boverseas (transfer|recipients)\b",
        r"\bcross[- ]border transfer\b",
    ],
    "data_subject_rights": [
        r"\baccess (and|or) correction (request|right)\b",
        r"\bright to access\b",
        r"\bdata subject (access )?request\b",
        r"\bdsar\b",
        r"\brequest .{0,20}(correction|access)\b",
    ],
}

SNIPPET_WINDOW = 120  # chars around each anchor hit
MAX_SNIPPETS_PER_CLAUSE = 4


@dataclass
class ClauseVerdict:
    clause: str            # one of CLAUSES
    present: bool          # final determination
    confidence: float      # 0..1
    evidence: str          # short redacted snippet supporting the verdict
    note: str = ""         # short rationale

    def to_dict(self) -> dict:
        return {
            "clause": self.clause,
            "present": self.present,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "note": self.note,
        }


def _strip_html(html: str) -> str:
    """Cheap tag-stripper that preserves text content. Avoids pulling in
    BeautifulSoup just for this; the LLM tolerates leftover whitespace."""
    if not html:
        return ""
    # Drop script/style blocks first
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def harvest_clause_snippets(policy_html: str) -> dict[str, list[str]]:
    """For each clause, harvest up to MAX_SNIPPETS_PER_CLAUSE windowed snippets
    from the privacy policy text. Returns {clause: [snippet, ...]}."""
    text = _strip_html(policy_html)
    if not text:
        return {clause: [] for clause in CLAUSES}

    text_lower = text.lower()
    out: dict[str, list[str]] = {clause: [] for clause in CLAUSES}

    for clause, anchors in _CLAUSE_ANCHORS.items():
        seen: set[str] = set()
        for pattern in anchors:
            for m in re.finditer(pattern, text_lower, re.IGNORECASE):
                start = max(0, m.start() - SNIPPET_WINDOW)
                end = min(len(text), m.end() + SNIPPET_WINDOW)
                snippet = text[start:end].strip()
                key = snippet[:160]
                if key not in seen and len(out[clause]) < MAX_SNIPPETS_PER_CLAUSE:
                    seen.add(key)
                    out[clause].append(snippet)
            if len(out[clause]) >= MAX_SNIPPETS_PER_CLAUSE:
                break
    return out


_CLASSIFIER_PROMPT = (
    "You are auditing a Singapore privacy policy for PDPA 2012 s.11/13/21/22/25/26 compliance. "
    "For each clause below, decide whether the supplied snippets demonstrate that the policy "
    "MEETS the obligation. Return ONLY a JSON object keyed by clause name:\n"
    "  {\"purpose\": {\"present\": bool, \"confidence\": 0.0-1.0, \"note\": \"short reason\"}, "
    "\"withdrawal\": {...}, \"dpo_contact\": {...}, \"retention\": {...}, "
    "\"third_party\": {...}, \"data_subject_rights\": {...}}\n"
    "Rules:\n"
    "  - present=true ONLY when the snippet actually fulfils the clause, not just mentions the word.\n"
    "  - Templated 'we may collect personal data' without enumerated purposes does NOT satisfy 'purpose'.\n"
    "  - A standalone DPO heading without contact details does NOT satisfy 'dpo_contact'.\n"
    "  - 'For as long as necessary' is acceptable retention language; absence of any time/condition is not.\n"
    "No commentary, no markdown."
)


# Multilingual classifier — same JSON schema, but the input is the raw policy
# text in any of Singapore's four official languages (English, Chinese, Malay,
# Tamil). We rely entirely on the LLM here because the English-only regex
# anchors in _CLAUSE_ANCHORS won't match Chinese/Malay/Tamil text. The model
# is expected to handle the multilingual reasoning and still return English
# JSON. Truncates input to avoid prompt-size blowups on long policies.
_MULTILINGUAL_PROMPT = (
    "You are auditing a Singapore privacy policy for PDPA 2012 s.11/13/21/22/25/26 "
    "compliance. The policy may be written in English, Chinese (zh), Malay (ms), or "
    "Tamil (ta). Read the full text and decide whether each required clause is present. "
    "Return ONLY a JSON object (in English) keyed by clause name:\n"
    "  {\"purpose\": {\"present\": bool, \"confidence\": 0.0-1.0, \"note\": \"short reason in English\"}, "
    "\"withdrawal\": {...}, \"dpo_contact\": {...}, \"retention\": {...}, "
    "\"third_party\": {...}, \"data_subject_rights\": {...}}\n"
    "Same rules as the English classifier: present=true only when the clause is actually "
    "fulfilled, not just mentioned. A standalone DPO heading without contact details "
    "does NOT satisfy 'dpo_contact'.\n"
    "No commentary, no markdown."
)

# Cap on policy text length sent to the multilingual LLM call — keeps prompt
# size and cost bounded for unusually long policies.
_MAX_POLICY_CHARS = 18_000


async def classify_clauses_multilingual(
    policy_text: str,
    language: str,
    provider: Optional[DeepSeekProvider] = None,
) -> list[ClauseVerdict]:
    """Classify a non-English privacy policy by feeding the raw text directly
    to the LLM with a multilingual prompt. Falls back to all-uncertain when
    no provider/API key is configured (we don't carry CN/MS/TA regex anchors).
    """
    stripped = _strip_html(policy_text)[:_MAX_POLICY_CHARS]
    if not stripped:
        return [
            ClauseVerdict(clause=c, present=False, confidence=0.6,
                          evidence="", note="Empty policy text")
            for c in CLAUSES
        ]

    if provider is None or not getattr(provider, "api_key", None):
        return [
            ClauseVerdict(clause=c, present=False, confidence=0.4,
                          evidence="",
                          note=f"Non-English ({language}) policy; no LLM available to classify")
            for c in CLAUSES
        ]

    user_payload = {"language": language, "policy_text": stripped}
    messages = [
        {"role": "system", "content": _MULTILINGUAL_PROMPT},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]
    raw = await provider.call_chat(messages)
    parsed = _parse_classification(raw)

    if not isinstance(parsed, dict):
        logger.warning(
            "Multilingual policy classifier: LLM returned non-dict for lang=%s; falling back to uncertain",
            language,
        )
        return [
            ClauseVerdict(clause=c, present=False, confidence=0.4,
                          evidence="",
                          note=f"Multilingual ({language}) LLM classification failed")
            for c in CLAUSES
        ]

    out: list[ClauseVerdict] = []
    for clause in CLAUSES:
        entry = parsed.get(clause) or {}
        present = bool(entry.get("present", False)) if isinstance(entry, dict) else False
        confidence = float(entry.get("confidence", 0.5)) if isinstance(entry, dict) else 0.5
        confidence = max(0.0, min(1.0, confidence))
        note = (entry.get("note") or "")[:200] if isinstance(entry, dict) else ""
        out.append(ClauseVerdict(
            clause=clause, present=present, confidence=confidence,
            evidence=f"[lang={language}] {stripped[:200]}",
            note=note,
        ))
    return out


async def classify_clauses(
    snippets_by_clause: dict[str, list[str]],
    provider: Optional[DeepSeekProvider] = None,
) -> list[ClauseVerdict]:
    """Classify each clause as present/absent using the LLM, falling back to
    a conservative heuristic when no provider/API key is available."""
    has_any_snippet = any(snippets_by_clause.get(c) for c in CLAUSES)

    if not has_any_snippet:
        return [
            ClauseVerdict(clause=c, present=False, confidence=0.6,
                          evidence="", note="No matching snippet harvested")
            for c in CLAUSES
        ]

    if provider is None or not getattr(provider, "api_key", None):
        return [_heuristic_verdict(c, snippets_by_clause.get(c, [])) for c in CLAUSES]

    user_payload = {
        "clauses": {c: snippets_by_clause.get(c, [])[:MAX_SNIPPETS_PER_CLAUSE] for c in CLAUSES}
    }
    messages = [
        {"role": "system", "content": _CLASSIFIER_PROMPT},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]
    raw = await provider.call_chat(messages)
    parsed = _parse_classification(raw)

    if not isinstance(parsed, dict):
        logger.warning("Policy classifier: LLM returned non-dict; falling back to heuristics")
        return [_heuristic_verdict(c, snippets_by_clause.get(c, [])) for c in CLAUSES]

    out: list[ClauseVerdict] = []
    for clause in CLAUSES:
        entry = parsed.get(clause) or {}
        snippets = snippets_by_clause.get(clause, [])
        present = bool(entry.get("present", False)) if isinstance(entry, dict) else False
        confidence = float(entry.get("confidence", 0.5)) if isinstance(entry, dict) else 0.5
        confidence = max(0.0, min(1.0, confidence))
        note = (entry.get("note") or "")[:200] if isinstance(entry, dict) else ""
        evidence = snippets[0][:200] if snippets else ""
        out.append(ClauseVerdict(
            clause=clause, present=present, confidence=confidence,
            evidence=evidence, note=note,
        ))
    return out


def _parse_classification(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _heuristic_verdict(clause: str, snippets: list[str]) -> ClauseVerdict:
    """Fallback used when no LLM is available.

    Strategy: clause is present if any snippet matched (we already filtered
    by clause-specific anchors) AND no obvious negation surrounds it. We mark
    confidence low so the score table reflects uncertainty.
    """
    if not snippets:
        return ClauseVerdict(clause=clause, present=False, confidence=0.55,
                             evidence="", note="No anchor matched in policy text")

    # Look for negation within ~40 chars of the anchor in the first snippet
    head = snippets[0].lower()
    if re.search(r"\b(do(es)? not|will not|never|no longer)\b[^.]{0,40}\b(provide|publish|disclose|store|collect|retain)\b", head):
        return ClauseVerdict(
            clause=clause, present=False, confidence=0.5,
            evidence=snippets[0][:200],
            note="Heuristic: negation phrasing near anchor",
        )
    return ClauseVerdict(
        clause=clause, present=True, confidence=0.5,
        evidence=snippets[0][:200],
        note="Heuristic: anchor matched without negation",
    )


CLAUSE_LABELS = {
    "purpose": "Purpose of Collection",
    "withdrawal": "Consent Withdrawal Mechanism",
    "dpo_contact": "DPO Contact Disclosure",
    "retention": "Retention Period",
    "third_party": "Third-Party / Overseas Transfer Disclosure",
    "data_subject_rights": "Data Subject Rights (Access & Correction)",
}


def summarise(verdicts: list[ClauseVerdict]) -> dict:
    """Roll per-clause verdicts into the dimension summary used by the PDF
    report and worker. Score is the % of required clauses confidently present."""
    if not verdicts:
        return {
            "score": 0,
            "status": "Non-Compliant",
            "present_count": 0,
            "total": len(CLAUSES),
            "missing": list(CLAUSES),
            "items": [],
        }

    present = [v for v in verdicts if v.present and v.confidence >= 0.5]
    missing = [v.clause for v in verdicts if not (v.present and v.confidence >= 0.5)]
    pct = round(100 * len(present) / len(CLAUSES))

    if pct >= 85:
        status = "Compliant"
    elif pct >= 50:
        status = "Partial"
    else:
        status = "Non-Compliant"

    return {
        "score": pct,
        "status": status,
        "present_count": len(present),
        "total": len(CLAUSES),
        "missing": missing,
        "items": [v.to_dict() for v in verdicts],
    }
