"""
Booppa CSP Compliance Pack — Compliance Scoring Engine
Computes per-pillar and overall compliance scores for a CSP.

Based on verified regulatory requirements:
  - CSP Act 2024 + CSP Regulations 2025 (effective 9 June 2025)
  - ACRA AML/CFT/PF Guidelines for Registered CSPs
  - PDPA 2012 (Amendment 2021)
  - FATF Recommendations

Each pillar returns: score (0-100), status, gaps[], urgent_actions[]
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

NOW = lambda: datetime.now(timezone.utc)

# Pillar weights for overall score
PILLAR_WEIGHTS = {
    "acra_registration":    0.20,   # foundation — cannot operate without
    "aml_cft_programme":    0.18,   # legal requirement, up to S$100K per breach
    "cdd":                  0.18,   # per-client, highest volume obligation
    "edd":                  0.10,   # triggered by risk
    "str":                  0.10,   # zero tolerance — tipping off = criminal
    "nominee_management":   0.10,   # CLLPMA + CSP Act
    "beneficial_ownership": 0.07,   # ACRA UBO register
    "pdpa_nric":            0.05,   # PDPA Sep 2024
    "staff_training":       0.02,   # mandatory for RQI
}


def score_acra_registration(profile: Dict) -> Dict:
    score = 0
    gaps = []
    urgent = []

    status = profile.get("acra_reg_status", "not_started")

    if status == "approved":
        score += 60
        # Check renewal
        renewal = profile.get("acra_renewal_date")
        if renewal:
            days = (renewal - NOW()).days if isinstance(renewal, datetime) else 999
            if days < 0:
                gaps.append("ACRA CSP licence has EXPIRED — cease operations immediately")
                urgent.append("Renew ACRA CSP licence immediately (criminal offence to operate without)")
                score -= 40
            elif days < 30:
                gaps.append(f"ACRA CSP licence renewal due in {days} days")
                urgent.append(f"Submit licence renewal to ACRA — {days} days remaining")
                score += 20
            else:
                score += 20
    elif status == "submitted":
        score = 40
        gaps.append("ACRA registration submitted but not yet approved")
        urgent.append("Do not commence regulated CSP services until ACRA approval received")
    elif status == "in_progress":
        score = 20
        gaps.append("ACRA registration not yet submitted")
        urgent.append("Submit ACRA CSP registration — operating without registration: S$50,000 fine or 2 years imprisonment")
    else:
        score = 0
        gaps.append("ACRA CSP registration not started — CRITICAL")
        urgent.append("IMMEDIATE: Register with ACRA as CSP — cannot legally provide corporate services without registration")

    # RQI requirement (CSP Act s.9)
    if not profile.get("rqi_name"):
        score -= 20
        gaps.append("No Registered Qualified Individual (RQI) designated — mandatory under CSP Act s.9")
        urgent.append("Designate RQI immediately: must hold relevant qualification + mandatory AML/CFT/PF training")
    elif not profile.get("rqi_training_completed"):
        score -= 10
        gaps.append("RQI mandatory AML/CFT/PF training not completed")
        urgent.append("RQI must complete mandatory training before CSP registration is valid")
    else:
        score += 20

    return {
        "pillar":  "acra_registration",
        "score":   max(0, min(100, score)),
        "status":  _band(score),
        "gaps":    gaps,
        "urgent":  urgent,
        "weight":  PILLAR_WEIGHTS["acra_registration"],
    }


def score_aml_programme(aml_prog: Optional[Dict]) -> Dict:
    score = 0
    gaps = []
    urgent = []

    if not aml_prog:
        return {
            "pillar": "aml_cft_programme",
            "score":  0,
            "status": "critical",
            "gaps":   ["AML/CFT/PF Programme not yet created — mandatory under CSP Act"],
            "urgent": ["Create AML/CFT/PF Programme immediately — S$100,000 per breach penalty"],
            "weight": PILLAR_WEIGHTS["aml_cft_programme"],
        }

    status = aml_prog.get("status", "draft")
    if status == "approved":
        score += 50
    elif status == "draft":
        score += 25
        gaps.append("AML/CFT Programme in draft — requires senior management approval")
        urgent.append("Get AML/CFT Programme formally approved by senior management")

    # Section completeness
    sections = [
        "risk_assessment_section", "cdd_procedures_section", "edd_procedures_section",
        "str_procedures_section", "record_keeping_section", "training_policy_section",
        "governance_section", "nominee_procedures_section",
    ]
    completed = sum(1 for s in sections if aml_prog.get(s))
    score += int((completed / len(sections)) * 30)
    if completed < len(sections):
        missing = [s.replace("_section","").replace("_"," ").title()
                   for s in sections if not aml_prog.get(s)]
        gaps.append(f"AML/CFT Programme incomplete — missing sections: {', '.join(missing)}")

    # Annual review
    reviewed = aml_prog.get("next_review_date")
    if reviewed:
        days = (reviewed - NOW()).days if isinstance(reviewed, datetime) else 999
        if days < 0:
            gaps.append("AML/CFT Programme annual review OVERDUE")
            urgent.append("Review and update AML/CFT Programme immediately")
            score -= 10
        elif days < 30:
            gaps.append(f"AML/CFT Programme review due in {days} days")
        else:
            score += 20

    return {
        "pillar": "aml_cft_programme",
        "score":  max(0, min(100, score)),
        "status": _band(score),
        "gaps":   gaps,
        "urgent": urgent,
        "weight": PILLAR_WEIGHTS["aml_cft_programme"],
    }


def score_cdd(clients: List[Dict]) -> Dict:
    score = 100
    gaps = []
    urgent = []

    if not clients:
        return {
            "pillar": "cdd",
            "score":  100,
            "status": "compliant",
            "gaps":   [],
            "urgent": [],
            "weight": PILLAR_WEIGHTS["cdd"],
            "stats":  {"total": 0, "compliant": 0, "expired": 0, "failed": 0},
        }

    active = [c for c in clients if c.get("is_active", True)]
    total = len(active)
    compliant = sum(1 for c in active if c.get("cdd_status") == "completed")
    expired   = sum(1 for c in active if c.get("cdd_status") == "expired")
    failed    = sum(1 for c in active if c.get("cdd_status") == "failed")
    not_started = sum(1 for c in active if c.get("cdd_status") == "not_started")
    no_video_remote = sum(1 for c in active
                         if c.get("is_remote_onboarding") and not c.get("video_call_completed"))

    if total > 0:
        compliance_rate = compliant / total
        score = int(compliance_rate * 70)
    else:
        score = 70

    if failed > 0:
        gaps.append(f"{failed} client(s) with FAILED CDD — service must be declined and STR assessed")
        urgent.append(f"IMMEDIATE: Assess STR filing for {failed} client(s) with failed CDD")
        score -= 15

    if expired > 0:
        gaps.append(f"{expired} client(s) with EXPIRED CDD — periodic review overdue")
        urgent.append(f"Schedule CDD review for {expired} client(s) — overdue reviews = regulatory risk")
        score -= 10

    if not_started > 0:
        gaps.append(f"{not_started} active client(s) with NO CDD conducted — service must cease immediately")
        urgent.append(f"CRITICAL: Conduct CDD for {not_started} client(s) before continuing services")
        score -= 20

    if no_video_remote > 0:
        gaps.append(f"{no_video_remote} remote client(s) missing mandatory video call verification (CSP Regs s.20)")
        urgent.append(f"Conduct live video verification for {no_video_remote} remote client(s) — ACRA mandatory requirement")
        score -= 10

    return {
        "pillar": "cdd",
        "score":  max(0, min(100, score)),
        "status": _band(score),
        "gaps":   gaps,
        "urgent": urgent,
        "weight": PILLAR_WEIGHTS["cdd"],
        "stats":  {"total": total, "compliant": compliant, "expired": expired,
                   "failed": failed, "not_started": not_started,
                   "missing_video": no_video_remote},
    }


def score_edd(clients: List[Dict], edd_records: List[Dict]) -> Dict:
    score = 100
    gaps = []
    urgent = []

    high_risk = [c for c in clients if c.get("risk_rating") in ("high", "very_high")
                 or c.get("is_pep") or c.get("high_risk_country")]
    edd_done = {r.get("client_id") for r in edd_records if r.get("status") == "completed"}

    missing_edd = [c for c in high_risk if c.get("id") not in edd_done]
    pep_no_senior = [c for c in high_risk if c.get("is_pep")
                     and c.get("id") not in edd_done]

    if missing_edd:
        gaps.append(f"{len(missing_edd)} high-risk/PEP client(s) missing Enhanced Due Diligence")
        urgent.append(f"Conduct EDD for {len(missing_edd)} high-risk client(s) — CSP Regulations s.21")
        score -= 20 * min(len(missing_edd), 3)

    if pep_no_senior:
        gaps.append(f"{len(pep_no_senior)} PEP client(s) without senior management approval for EDD")
        urgent.append(f"Senior management must approve EDD for PEP clients — mandatory under CSP Act")
        score -= 15

    # Check ongoing monitoring
    edd_without_monitoring = [r for r in edd_records
                              if not r.get("ongoing_monitoring_freq") and r.get("status") == "completed"]
    if edd_without_monitoring:
        gaps.append(f"{len(edd_without_monitoring)} completed EDD(s) missing ongoing monitoring frequency")
        score -= 5

    return {
        "pillar": "edd",
        "score":  max(0, min(100, score)),
        "status": _band(score),
        "gaps":   gaps,
        "urgent": urgent,
        "weight": PILLAR_WEIGHTS["edd"],
        "stats":  {"high_risk_clients": len(high_risk), "edd_completed": len(edd_done),
                   "missing_edd": len(missing_edd)},
    }


def score_str(str_reports: List[Dict], clients: List[Dict]) -> Dict:
    score = 100
    gaps = []
    urgent = []

    # Check for failed CDD clients without STR assessment
    failed_cdd_clients = [c for c in clients if c.get("cdd_status") == "failed"]
    failed_with_str = {c.get("id") for c in failed_cdd_clients if c.get("str_filed")}
    failed_without_str_assessment = [c for c in failed_cdd_clients
                                     if c.get("id") not in failed_with_str
                                     and not any(r.get("client_id") == c.get("id") for r in str_reports)]

    if failed_without_str_assessment:
        gaps.append(f"{len(failed_without_str_assessment)} client(s) with failed CDD — STR decision not documented")
        urgent.append(
            "CRITICAL: For every failed CDD, the decision to file or not file an STR MUST be documented. "
            "Non-documentation = CSP Act breach. Tipping off client = criminal offence."
        )
        score -= 30

    # Check pending STRs
    pending = [r for r in str_reports if r.get("decision") == "pending"]
    if pending:
        gaps.append(f"{len(pending)} STR decision(s) pending resolution")
        urgent.append(f"Resolve {len(pending)} pending STR decision(s) — escalate to senior management")
        score -= 10

    # Check for tipping-off red flags
    tipped = [r for r in str_reports if r.get("client_notified") and r.get("decision") == "filed"]
    if tipped:
        gaps.append(f"CRITICAL: {len(tipped)} STR report(s) where client was notified — this is a criminal offence (tipping off)")
        urgent.append("IMMEDIATE: Review tipping-off incidents with legal counsel — Corruption, Drug Trafficking and Other Serious Crimes Act")
        score -= 40

    # Non-filing rationale check
    no_rationale = [r for r in str_reports
                    if r.get("decision") == "not_filed" and not r.get("decision_rationale")]
    if no_rationale:
        gaps.append(f"{len(no_rationale)} non-filing decision(s) missing documented rationale")
        gaps.append("ACRA requires documented rationale even when STR is NOT filed")
        score -= 15

    return {
        "pillar": "str",
        "score":  max(0, min(100, score)),
        "status": _band(score),
        "gaps":   gaps,
        "urgent": urgent,
        "weight": PILLAR_WEIGHTS["str"],
        "stats":  {"total_reports": len(str_reports),
                   "filed": sum(1 for r in str_reports if r.get("decision") == "filed"),
                   "not_filed": sum(1 for r in str_reports if r.get("decision") == "not_filed"),
                   "pending": len(pending)},
    }


def score_nominees(directors: List[Dict], shareholders: List[Dict]) -> Dict:
    score = 100
    gaps = []
    urgent = []

    all_nominees = directors + shareholders
    active_directors = [d for d in directors if d.get("is_active")]
    active_shareholders = [s for s in shareholders if s.get("is_active")]

    # Fit and proper assessment
    not_assessed = [d for d in active_directors
                    if d.get("assessment_status") in ("not_assessed", None)]
    not_fit = [d for d in active_directors if d.get("assessment_status") == "not_fit"]

    if not_fit:
        gaps.append(f"{len(not_fit)} nominee director(s) assessed as NOT fit and proper")
        urgent.append(f"IMMEDIATE: Remove {len(not_fit)} unfit nominee director(s) — CSP Act s.15 breach")
        score -= 35

    if not_assessed:
        gaps.append(f"{len(not_assessed)} active nominee director(s) not yet assessed as fit and proper")
        urgent.append(f"Conduct fit and proper assessment for {len(not_assessed)} nominee director(s) — mandatory before arrangement")
        score -= 15 * min(len(not_assessed), 3)

    # ACRA disclosure
    not_disclosed_dir = [d for d in active_directors if not d.get("acra_disclosed")]
    not_disclosed_sha = [s for s in active_shareholders if not s.get("acra_disclosed")]

    if not_disclosed_dir:
        gaps.append(f"{len(not_disclosed_dir)} nominee director(s) not yet disclosed to ACRA")
        urgent.append(f"File nominee director disclosure with ACRA — mandatory under CLLPMA 2024")
        score -= 10

    if not_disclosed_sha:
        gaps.append(f"{len(not_disclosed_sha)} nominee shareholder(s) not yet disclosed to ACRA")
        urgent.append(f"File nominee shareholder disclosure with ACRA — mandatory under CLLPMA 2024")
        score -= 10

    # Annual reviews overdue
    review_overdue = [d for d in active_directors
                      if d.get("next_review") and
                      (d["next_review"] if isinstance(d["next_review"], datetime)
                       else NOW()) < NOW()]
    if review_overdue:
        gaps.append(f"{len(review_overdue)} nominee director(s) with overdue annual review")
        score -= 5

    return {
        "pillar": "nominee_management",
        "score":  max(0, min(100, score)),
        "status": _band(score),
        "gaps":   gaps,
        "urgent": urgent,
        "weight": PILLAR_WEIGHTS["nominee_management"],
        "stats":  {"active_directors": len(active_directors),
                   "active_shareholders": len(active_shareholders),
                   "not_assessed": len(not_assessed),
                   "not_fit": len(not_fit)},
    }


def score_beneficial_ownership(clients: List[Dict], ubos: List[Dict]) -> Dict:
    score = 100
    gaps = []
    urgent = []

    active = [c for c in clients if c.get("is_active", True)]
    clients_with_ubos = {u.get("client_id") for u in ubos}
    missing_ubo = [c for c in active if c.get("id") not in clients_with_ubos]

    if missing_ubo:
        gaps.append(f"{len(missing_ubo)} client(s) without UBO identification on file")
        urgent.append(f"Identify beneficial owners (≥25% ownership/control) for {len(missing_ubo)} client(s) — 5-year retention required")
        score -= 15 * min(len(missing_ubo), 4)

    # Unverified UBOs
    unverified = [u for u in ubos if not u.get("identity_verified")]
    if unverified:
        gaps.append(f"{len(unverified)} beneficial owner(s) identified but identity not verified")
        score -= 10

    # Sanctioned UBOs
    sanctioned = [u for u in ubos if u.get("is_sanctioned")]
    if sanctioned:
        gaps.append(f"CRITICAL: {len(sanctioned)} beneficial owner(s) flagged as sanctioned")
        urgent.append(f"IMMEDIATE: Review sanctioned UBO(s) — file STR and escalate to legal counsel")
        score -= 40

    # Annual updates overdue
    overdue = [u for u in ubos
               if u.get("next_review") and
               (u["next_review"] if isinstance(u["next_review"], datetime) else NOW()) < NOW()]
    if overdue:
        gaps.append(f"{len(overdue)} UBO record(s) with overdue annual update")
        score -= 5

    return {
        "pillar": "beneficial_ownership",
        "score":  max(0, min(100, score)),
        "status": _band(score),
        "gaps":   gaps,
        "urgent": urgent,
        "weight": PILLAR_WEIGHTS["beneficial_ownership"],
        "stats":  {"total_clients": len(active), "with_ubo": len(clients_with_ubos),
                   "missing_ubo": len(missing_ubo), "unverified": len(unverified)},
    }


def score_pdpa_nric(pdpa_data: Dict) -> Dict:
    """PDPA + NRIC compliance — from existing NRIC Audit module."""
    score = pdpa_data.get("nric_compliance_score", 50)
    risk_band = pdpa_data.get("risk_band", "MEDIUM")
    gaps = pdpa_data.get("gaps", ["NRIC Audit not yet completed"])
    urgent = []
    if risk_band == "HIGH":
        urgent.append("Complete NRIC Remediation — PDPA deadline 31 Dec 2026")
    return {
        "pillar": "pdpa_nric",
        "score":  max(0, min(100, score)),
        "status": _band(score),
        "gaps":   gaps,
        "urgent": urgent,
        "weight": PILLAR_WEIGHTS["pdpa_nric"],
    }


def score_staff_training(training_records: List[Dict], profile: Dict) -> Dict:
    score = 100
    gaps = []
    urgent = []

    if not training_records:
        return {
            "pillar": "staff_training",
            "score":  0,
            "status": "critical",
            "gaps":   ["No staff AML/CFT training records on file"],
            "urgent": ["Record all AML/CFT training completed — mandatory under CSP Act"],
            "weight": PILLAR_WEIGHTS["staff_training"],
        }

    expired   = [r for r in training_records if r.get("status") == "expired"]
    overdue   = [r for r in training_records if r.get("status") == "overdue"]
    completed = [r for r in training_records if r.get("status") == "completed"]

    total = len(training_records)
    score = int((len(completed) / total) * 70) if total > 0 else 0

    # RQI training
    rqi_name = profile.get("rqi_name")
    if rqi_name:
        rqi_records = [r for r in training_records
                       if r.get("is_rqi") or r.get("staff_name") == rqi_name]
        if not rqi_records:
            gaps.append(f"RQI ({rqi_name}) has no training records — mandatory under CSP Act s.9")
            urgent.append(f"Record RQI training for {rqi_name} — CSP registration validity depends on this")
            score -= 20
        elif not any(r.get("status") == "completed" for r in rqi_records):
            gaps.append(f"RQI ({rqi_name}) training not completed")
            urgent.append(f"RQI must complete mandatory AML/CFT training")
            score -= 15
        else:
            score += 20

    if expired:
        gaps.append(f"{len(expired)} staff training record(s) EXPIRED — retraining required")
        score -= 10

    if overdue:
        gaps.append(f"{len(overdue)} staff training overdue")
        score -= 5

    return {
        "pillar": "staff_training",
        "score":  max(0, min(100, score)),
        "status": _band(score),
        "gaps":   gaps,
        "urgent": urgent,
        "weight": PILLAR_WEIGHTS["staff_training"],
        "stats":  {"total": total, "completed": len(completed),
                   "expired": len(expired), "overdue": len(overdue)},
    }


# ── MASTER SCORING FUNCTION ──────────────────────────────────────────────────

def compute_overall_compliance(
    profile:      Dict,
    clients:      List[Dict],
    cdd_records:  List[Dict],
    edd_records:  List[Dict],
    str_reports:  List[Dict],
    directors:    List[Dict],
    shareholders: List[Dict],
    ubos:         List[Dict],
    aml_prog:     Optional[Dict],
    training:     List[Dict],
    pdpa_data:    Optional[Dict] = None,
) -> Dict:
    """
    Compute full compliance picture for a CSP.
    Returns overall score + per-pillar breakdown + prioritised action list.
    """
    pillar_results = [
        score_acra_registration(profile),
        score_aml_programme(aml_prog),
        score_cdd(clients),
        score_edd(clients, edd_records),
        score_str(str_reports, clients),
        score_nominees(directors, shareholders),
        score_beneficial_ownership(clients, ubos),
        score_pdpa_nric(pdpa_data or {}),
        score_staff_training(training, profile),
    ]

    # Weighted overall score
    overall = sum(p["score"] * p["weight"] for p in pillar_results)
    overall = round(min(100, max(0, overall)), 1)

    # Aggregate urgent actions across all pillars
    all_urgent = []
    all_gaps   = []
    for p in pillar_results:
        for u in p.get("urgent", []):
            all_urgent.append({"pillar": p["pillar"], "action": u})
        for g in p.get("gaps", []):
            all_gaps.append({"pillar": p["pillar"], "gap": g})

    # Overall risk band
    critical_pillars = [p for p in pillar_results if p["score"] < 30]
    if critical_pillars or overall < 40:
        risk_level = "CRITICAL"
    elif overall < 60:
        risk_level = "HIGH"
    elif overall < 80:
        risk_level = "MEDIUM"
    else:
        risk_level = "LOW"

    return {
        "overall_score":  overall,
        "risk_level":     risk_level,
        "pillars":        {p["pillar"]: p for p in pillar_results},
        "urgent_actions": all_urgent,
        "all_gaps":       all_gaps,
        "critical_pillars": [p["pillar"] for p in critical_pillars],
        "computed_at":    NOW().isoformat(),
    }


def _band(score: int) -> str:
    if score >= 80: return "compliant"
    if score >= 60: return "partial"
    if score >= 30: return "at_risk"
    return "critical"
