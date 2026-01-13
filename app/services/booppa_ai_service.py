"""
Booppa AI Service - Specialized for Singapore compliance auditing
Zero-cost training through prompt engineering and templates
"""

import json
import os
from datetime import datetime
from typing import Dict, List, Optional
import logging

logger = logging.getLogger(__name__)

# ============================================
# BOOPPA SYSTEM PROMPT - The "Booppa Persona"
# ============================================

BOOPPA_SYSTEM_PROMPT = """You are BOOPPA AI - Singapore's leading compliance auditor specializing in PDPA, MTCS, and MAS regulations.

CRITICAL INSTRUCTIONS:
1. ALWAYS cite specific legislation sections (PDPA Section 13, MAS Notice 626, etc.)
2. ALWAYS mention exact penalties (S$1,000,000, up to 10% of annual revenue)
3. ALWAYS provide actionable steps with deadlines
4. ALWAYS reference PDPC Advisory Guidelines when relevant
5. Structure responses with: VIOLATION → PENALTY → ACTION → REFERENCE
6. Use Singapore-specific terminology: NRIC (not ID), PDPC (not DPA), DNC Registry
7. Include Chinese/Malay/Tamil translations for key terms when helpful
8. Emphasize court-admissibility of blockchain evidence
9. Format with clear severity levels: CRITICAL, HIGH, MEDIUM, LOW
10. Provide Polygonscan verification instructions for blockchain evidence

SPECIALIZED KNOWLEDGE:
- PDPA 2012 (Personal Data Protection Act Singapore)
- PDPC Advisory Guidelines (NRIC 2018, Cookies 2021, Accountability 2021)
- MAS Technology Risk Management Guidelines
- MTCS Level 3 requirements (Tier 3)
- Singapore Cybersecurity Act 2018
- Do Not Call (DNC) Registry rules
- Personal Data Protection Commission (PDPC) enforcement history
- Monetary Authority of Singapore (MAS) regulatory framework

RESPONSE TEMPLATE:
[SEVERITY: CRITICAL/HIGH/MEDIUM/LOW]
[VIOLATION]: Clear description of compliance issue
[LEGISLATION]: Specific sections violated (PDPA Section X, MAS Notice Y)
[PENALTY]: Exact financial penalty or regulatory consequence
[IMMEDIATE ACTION]: Concrete steps required within 24-48 hours
[COMPLIANCE DEADLINE]: Realistic timeline for full compliance (7-30 days)
[REFERENCE]: Official PDPC/MAS/MTCS documentation links
[BLOCKCHAIN EVIDENCE]: How to document compliance on Polygon for court-admissibility
[VERIFICATION]: Polygonscan URL format for evidence verification

EXAMPLE OUTPUT:
CRITICAL VIOLATION: Unauthorized NRIC Collection
LEGISLATION: PDPA Section 18, PDPC Advisory Guidelines 2018
PENALTY: Up to S$1,000,000
IMMEDIATE ACTION (24h): Remove NRIC collection form from website
COMPLIANCE DEADLINE: 7 days
REFERENCE: https://www.pdpc.gov.sg/guidelines-and-consultation/2018/01/advisory-guidelines-for-nric-numbers
BLOCKCHAIN EVIDENCE: Anchor removal timestamp on Polygon Mainnet (tx: 0x...)
VERIFICATION: https://polygonscan.com/tx/0x... (include QR code in report)
"""

# ============================================
# SINGAPORE LEGISLATION DATABASE
# ============================================

SINGAPORE_LEGISLATION = {
    "PDPA": {
        "sections": {
            "11": {
                "title": "Openness Obligation",
                "description": "Must publish data protection policies",
            },
            "13": {
                "title": "Consent Obligation",
                "description": "Requires clear affirmative consent",
            },
            "14": {
                "title": "Notification Obligation",
                "description": "Inform purpose of collection",
            },
            "18": {
                "title": "Purpose Limitation",
                "description": "Use only for stated purpose",
            },
            "24": {
                "title": "Protection Obligation",
                "description": "Implement reasonable security",
            },
            "25": {
                "title": "Retention Limitation",
                "description": "Delete when no longer needed",
            },
            "26": {
                "title": "Transfer Limitation",
                "description": "Ensure overseas protection standards",
            },
        },
        "penalties": {
            "tier1": "Up to S$1,000,000",
            "tier2": "Up to 10% of annual turnover in Singapore",
        },
    },
    "PDPC_ADVISORIES": {
        "nric_2018": {
            "title": "Advisory Guidelines on NRIC Numbers (2018)",
            "summary": "Organizations should not collect NRIC unless required by law",
            "url": "https://www.pdpc.gov.sg/guidelines-and-consultation/2018/01/advisory-guidelines-for-nric-numbers",
            "key_points": [
                "NRIC should not be default identifier",
                "Only collect when required by law",
                "Must justify collection clearly",
                "Consider alternatives (last 4 digits)",
            ],
        },
        "cookies_2021": {
            "title": "Guide to Enhanced Notice and Choice (2021)",
            "summary": "Requires active consent, no implied consent allowed",
            "url": "https://www.pdpc.gov.sg/guidelines-and-consultation/2021/01/guide-to-enhanced-notice-and-choice",
            "key_points": [
                "Active opt-in required",
                "No pre-ticked boxes",
                "Granular consent options",
                "Clear withdrawal mechanism",
            ],
        },
        "accountability_2021": {
            "title": "Guide to Data Protection by Design (2021)",
            "summary": "Privacy must be embedded into systems from the start",
            "url": "https://www.pdpc.gov.sg/guidelines-and-consultation/2021/01/guide-to-data-protection-by-design",
            "key_points": [
                "Proactive not reactive",
                "Privacy as default setting",
                "Full lifecycle protection",
                "Visibility and transparency",
            ],
        },
    },
    "MAS_NOTICES": {
        "626": {
            "title": "Technology Risk Management",
            "scope": "Financial institutions",
        },
        "644": {"title": "Cyber Hygiene", "scope": "All regulated entities"},
        "655": {
            "title": "Third Party Risk Management",
            "scope": "Outsourcing arrangements",
        },
    },
    "MTCS_LEVELS": {
        "1": {"name": "Basic", "description": "Low impact information"},
        "2": {"name": "Enhanced", "description": "Medium impact information"},
        "3": {
            "name": "High",
            "description": "High impact systems (financial, healthcare)",
        },
    },
}

# ============================================
# HELPER FUNCTIONS
# ============================================


def get_penalty_for_violation(violation_type: str) -> Dict:
    """Get specific penalty information for violation type"""
    penalties = {
        "nric_collection": {
            "amount": "Up to S$1,000,000",
            "legislation": "PDPA Section 18",
            "reference": "PDPC Advisory Guidelines 2018",
        },
        "no_consent": {
            "amount": "Up to S$1,000,000",
            "legislation": "PDPA Section 13",
            "reference": "PDPC Guide to Enhanced Notice 2021",
        },
        "data_breach": {
            "amount": "Up to S$1,000,000 or 10% annual turnover",
            "legislation": "PDPA Section 24",
            "reference": "Cybersecurity Act 2018",
        },
        "dnc_violation": {
            "amount": "Up to S$10,000 per message",
            "legislation": "DNC Registry",
            "reference": "Spam Control Act",
        },
        "no_https": {
            "amount": "Up to S$1,000,000",
            "legislation": "PDPA Section 24",
            "reference": "PDPC Guide to Data Protection by Design",
        },
    }
    return penalties.get(
        violation_type,
        {
            "amount": "Up to S$1,000,000",
            "legislation": "PDPA General",
            "reference": "Consult legal counsel",
        },
    )


def get_compliance_deadline(severity: str) -> str:
    """Get realistic compliance deadlines based on severity"""
    deadlines = {
        "CRITICAL": "24-48 hours for immediate action, 7 days for full compliance",
        "HIGH": "48-72 hours for immediate action, 14 days for full compliance",
        "MEDIUM": "7 days for initial action, 30 days for full compliance",
        "LOW": "14 days for initial action, 60 days for full compliance",
    }
    return deadlines.get(
        severity, "7 days for initial action, 30 days for full compliance"
    )


def calculate_risk_score(violations: List[Dict]) -> int:
    """Calculate risk score (0-100) from violations"""
    severity_weights = {"CRITICAL": 10, "HIGH": 7, "MEDIUM": 4, "LOW": 1}

    total_score = 0
    for violation in violations:
        severity = violation.get("severity", "MEDIUM")
        total_score += severity_weights.get(severity, 0)

    # Scale to 100 with diminishing returns
    risk_score = min(100, total_score * 8)
    return risk_score


def get_risk_level(score: int) -> Dict:
    """Convert score to risk level with description"""
    if score >= 80:
        return {
            "level": "CRITICAL",
            "color": "#dc3545",
            "description": "Immediate action required",
        }
    elif score >= 60:
        return {
            "level": "HIGH",
            "color": "#fd7e14",
            "description": "Urgent attention needed",
        }
    elif score >= 40:
        return {
            "level": "MEDIUM",
            "color": "#ffc107",
            "description": "Address within 30 days",
        }
    elif score >= 20:
        return {
            "level": "LOW",
            "color": "#17a2b8",
            "description": "Monitor and plan fixes",
        }
    else:
        return {
            "level": "MINIMAL",
            "color": "#28a745",
            "description": "Maintain current practices",
        }


# ============================================
# MAIN BOOPPA AI SERVICE CLASS
# ============================================


class BooppaAIService:
    """Enhanced AI service with Booppa-specific training via prompt engineering"""

    def __init__(self, deepseek_api_key: str = None):
        self.system_prompt = BOOPPA_SYSTEM_PROMPT
        self.legislation = SINGAPORE_LEGISLATION
        self.prompts = self._load_prompts()
        self.deepseek_api_key = deepseek_api_key

    def _load_prompts(self) -> Dict:
        """Load Booppa-specific prompt templates"""
        prompts_path = "app/services/prompts/pdpa_prompts.json"

        if os.path.exists(prompts_path):
            try:
                with open(prompts_path, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Failed to load prompts: {e}")

        # Return default prompts if file doesn't exist
        return self._get_default_prompts()

    def _get_default_prompts(self) -> Dict:
        """Default Booppa prompts for common violations"""
        return {
            "nric_violation": {
                "template": """CRITICAL VIOLATION: Unauthorized NRIC Collection

LEGISLATION VIOLATED:
• PDPA Section 18 - Purpose Limitation
• PDPC Advisory Guidelines on NRIC Numbers (2018)

PENALTY: {penalty_amount}

VIOLATION DETAILS:
{details}

IMMEDIATE ACTIONS REQUIRED (within 24 hours):
1. Remove NRIC collection field from {location}
2. Review all data collection forms for unnecessary NRIC fields
3. Document the removal with timestamp

ALTERNATIVES TO NRIC COLLECTION:
• Last 4 digits of NRIC + Full name
• Company-issued identification number
• Membership or loyalty card number
• Email + Phone verification

COMPLIANCE DEADLINE: {deadline}

REFERENCE DOCUMENTS:
• PDPC Advisory Guidelines: {nric_guidelines_url}
• MAS Notice 626 for financial institutions
• Healthcare Services Act for medical providers

BLOCKCHAIN EVIDENCE REQUIREMENTS:
1. Generate SHA-256 hash of NRIC removal confirmation
2. Anchor hash on Polygon Mainnet for timestamp proof
3. Store transaction hash: {tx_hash_placeholder}
4. Include Polygonscan QR code in compliance report

VERIFICATION:
• Transaction: https://polygonscan.com/tx/{tx_hash_placeholder}
• Court-admissible as per Singapore Evidence Act""",
                "severity": "CRITICAL",
                "triggers": [
                    "nric",
                    "national registration identity card",
                    "fin number",
                ],
            },
            "cookie_violation": {
                "template": """HIGH VIOLATION: Non-Compliant Cookie Consent Mechanism

LEGISLATION VIOLATED:
• PDPA Section 13 - Consent Obligation
• PDPC Guide to Enhanced Notice and Choice (2021)

PENALTY: {penalty_amount}

VIOLATION DETAILS:
{details}

REQUIRED FIXES (within 48 hours):
1. Implement affirmative opt-in mechanism (no pre-ticked boxes)
2. Provide granular consent options:
   • Essential cookies (required for functionality)
   • Analytics cookies (performance tracking)
   • Marketing cookies (advertising preferences)
3. Ensure banner is prominent and persistent until choice made
4. Include options in 4 official languages:
   • English • Chinese (中文) • Malay (Bahasa Melayu) • Tamil (தமிழ்)
5. Implement easy withdrawal mechanism

COMPLIANCE DEADLINE: {deadline}

REFERENCE DOCUMENTS:
• PDPC Guide to Enhanced Notice: {cookies_guidelines_url}
• Guide to Active Consent for Online Activities

BLOCKCHAIN EVIDENCE:
1. Document consent flow implementation
2. Anchor consent mechanism screenshots on Polygon
3. Timestamp deployment of compliant solution

NOTE: Implied consent (continued browsing) is NOT sufficient under PDPA""",
                "severity": "HIGH",
                "triggers": [
                    "cookie banner",
                    "consent",
                    "gdpr popup",
                    "tracking consent",
                ],
            },
            "security_violation": {
                "template": """CRITICAL VIOLATION: Inadequate Data Protection Measures

LEGISLATION VIOLATED:
• PDPA Section 24 - Protection Obligation
• MAS Notice 644 - Cyber Hygiene
• MTCS Level 3 Requirements (if applicable)

PENALTY: {penalty_amount}

VIOLATION DETAILS:
{details}

MANDATORY SECURITY IMPLEMENTATION (within 72 hours):
1. DEPLOY HTTPS IMMEDIATELY (Critical for data in transit)
2. Implement security headers:
   • Strict-Transport-Security (HSTS)
   • Content-Security-Policy (CSP)
   • X-XSS-Protection
   • X-Content-Type-Options
3. Conduct vulnerability assessment
4. Document security measures in privacy policy

ADDITIONAL RECOMMENDATIONS:
• Implement Web Application Firewall (WAF)
• Regular security patching schedule
• Employee security awareness training
• Incident response plan

COMPLIANCE DEADLINE: {deadline}

REFERENCE DOCUMENTS:
• PDPC Guide to Data Protection by Design
• MAS Technology Risk Management Guidelines
• Singapore Cybersecurity Act 2018

BLOCKCHAIN EVIDENCE:
1. Anchor security implementation certificates
2. Document compliance timeline
3. Store security assessment reports with timestamp""",
                "severity": "CRITICAL",
                "triggers": [
                    "http://",
                    "no ssl",
                    "missing security headers",
                    "insecure connection",
                ],
            },
        }

    async def generate_compliance_report(self, scan_data: Dict) -> Dict:
        """
        Generate complete compliance report with Booppa-specific formatting

        Args:
            scan_data: Dictionary containing scan results from SingaporeScanner

        Returns:
            Dict: Complete compliance report ready for PDF generation
        """
        logger.info(
            f"Generating compliance report for {scan_data.get('company_name', 'unknown')}"
        )

        # Detect violations from scan data
        violations = self._detect_violations(scan_data)

        # Calculate risk metrics
        risk_score = calculate_risk_score(violations)
        risk_level = get_risk_level(risk_score)

        # Generate report sections
        executive_summary = self._generate_executive_summary(violations, risk_level)
        detailed_findings = []

        for violation in violations:
            finding = await self._generate_violation_detail(violation, scan_data)
            detailed_findings.append(finding)

        # Generate recommendations
        recommendations = self._generate_recommendations(violations)

        # Blockchain evidence instructions
        blockchain_evidence = self._generate_blockchain_instructions(
            scan_data, violations
        )

        # Create complete report
        report = {
            "report_metadata": {
                "report_id": f"BOOPPA-{datetime.now().strftime('%Y%m%d')}-{abs(hash(scan_data.get('url', '')) % 10000):04d}",
                "generated_date": datetime.now().strftime("%d %B %Y"),
                "generated_time": datetime.now().strftime("%H:%M:%S"),
                "version": "2.0",
                "ai_model": "DeepSeek-Chat with Booppa Specialization",
            },
            "company_info": {
                "name": scan_data.get("company_name", "Not specified"),
                "website": scan_data.get("url", "Not specified"),
                "scan_date": scan_data.get(
                    "scan_date", datetime.now().strftime("%Y-%m-%d")
                ),
            },
            "executive_summary": executive_summary,
            "risk_assessment": {
                "score": risk_score,
                "level": risk_level["level"],
                "color": risk_level["color"],
                "description": risk_level["description"],
                "breakdown": self._get_risk_breakdown(violations),
            },
            "detailed_findings": detailed_findings,
            "recommendations": recommendations,
            "blockchain_evidence": blockchain_evidence,
            "legal_references": self._get_relevant_references(violations),
            "next_steps": self._generate_next_steps(violations),
            "disclaimer": self._get_disclaimer(),
        }

        return report

    def _detect_violations(self, scan_data: Dict) -> List[Dict]:
        """Detect compliance violations from scan data"""
        violations = []

        # NRIC Collection Check
        if scan_data.get("collects_nric") and not scan_data.get(
            "has_legal_justification"
        ):
            violations.append(
                {
                    "type": "nric_violation",
                    "severity": "CRITICAL",
                    "details": "NRIC collection detected without clear legal justification",
                    "location": scan_data.get("url", "website"),
                    "evidence": scan_data.get(
                        "nric_evidence", "Form fields collecting NRIC/FIN"
                    ),
                }
            )

        # HTTPS Security Check
        if not scan_data.get("uses_https", False):
            violations.append(
                {
                    "type": "security_violation",
                    "severity": "CRITICAL",
                    "details": "Website does not use HTTPS encryption - data transmission is insecure",
                    "location": scan_data.get("url", "website"),
                    "evidence": f"HTTP protocol detected at {scan_data.get('url')}",
                }
            )

        # Cookie Consent Check
        cookie_check = scan_data.get("consent_mechanism", {})
        if not cookie_check.get("has_cookie_banner", False):
            violations.append(
                {
                    "type": "cookie_violation",
                    "severity": "HIGH",
                    "details": "Missing cookie consent banner - implied consent not compliant with PDPA",
                    "location": scan_data.get("url", "website"),
                    "evidence": "No cookie consent mechanism detected",
                }
            )
        elif not cookie_check.get("has_active_consent", False):
            violations.append(
                {
                    "type": "cookie_violation",
                    "severity": "HIGH",
                    "details": "Cookie banner present but lacks active consent mechanism",
                    "location": scan_data.get("url", "website"),
                    "evidence": "Passive or implied consent detected",
                }
            )

        # DPO Check
        dpo_check = scan_data.get("dpo_compliance", {})
        if not dpo_check.get("has_dpo", False):
            violations.append(
                {
                    "type": "organizational_violation",
                    "severity": "MEDIUM",
                    "details": "No Data Protection Officer (DPO) information identified",
                    "location": "Organization",
                    "evidence": "Missing DPO contact in privacy policy or website",
                }
            )

        # DNC Registry Mention
        if not scan_data.get("dnc_mention", {}).get("mentions_dnc", False):
            violations.append(
                {
                    "type": "marketing_violation",
                    "severity": "MEDIUM",
                    "details": "No mention of DNC Registry compliance for marketing communications",
                    "location": "Privacy Policy / Marketing terms",
                    "evidence": "DNC Registry not referenced",
                }
            )

        return violations

    async def _generate_violation_detail(
        self, violation: Dict, scan_data: Dict
    ) -> Dict:
        """Generate detailed violation report using templates"""
        violation_type = violation.get("type")

        # Get penalty information
        penalty_info = get_penalty_for_violation(violation_type)

        # Get compliance deadline
        deadline = get_compliance_deadline(violation.get("severity", "MEDIUM"))

        # Check if we have a template for this violation
        if violation_type in self.prompts:
            template = self.prompts[violation_type]["template"]

            # Fill template with data
            description = template.format(
                details=violation.get("details", "No specific details provided"),
                location=violation.get("location", scan_data.get("url", "the website")),
                penalty_amount=penalty_info["amount"],
                deadline=deadline,
                nric_guidelines_url=SINGAPORE_LEGISLATION["PDPC_ADVISORIES"][
                    "nric_2018"
                ]["url"],
                cookies_guidelines_url=SINGAPORE_LEGISLATION["PDPC_ADVISORIES"][
                    "cookies_2021"
                ]["url"],
                tx_hash_placeholder="0x"
                + "0" * 64,  # Placeholder for actual transaction
                company_name=scan_data.get("company_name", "the organization"),
                scan_date=scan_data.get(
                    "scan_date", datetime.now().strftime("%Y-%m-%d")
                ),
            )
        else:
            # Fallback to generic AI generation if no template
            description = await self._generate_generic_violation(violation, scan_data)

        return {
            "type": violation_type,
            "severity": violation.get("severity", "MEDIUM"),
            "description": description,
            "evidence": violation.get("evidence", "Automated scan detection"),
            "penalty": penalty_info,
            "deadline": deadline,
            "legislation_references": self._get_violation_legislation(violation_type),
            "priority": self._get_priority_level(violation.get("severity")),
        }

    async def _generate_generic_violation(
        self, violation: Dict, scan_data: Dict
    ) -> str:
        """Generate violation description using AI fallback"""
        # This would call DeepSeek API if available
        # For now, return a structured generic response

        return f"""{violation.get('severity', 'MEDIUM')} VIOLATION: {violation.get('details', 'Compliance issue detected')}

Legislation: PDPA General Provisions
Penalty: Up to S$1,000,000
Location: {violation.get('location', 'Website')}
Evidence: {violation.get('evidence', 'Automated scan')}

Recommended Actions:
1. Review compliance requirements
2. Consult PDPC guidelines
3. Implement corrective measures
4. Document compliance steps

Compliance Deadline: {get_compliance_deadline(violation.get('severity', 'MEDIUM'))}

Note: Consult legal counsel for specific compliance requirements."""

    def _generate_executive_summary(
        self, violations: List[Dict], risk_level: Dict
    ) -> str:
        """Generate executive summary of the compliance report"""

        critical_count = sum(1 for v in violations if v.get("severity") == "CRITICAL")
        high_count = sum(1 for v in violations if v.get("severity") == "HIGH")
        total_count = len(violations)

        summary = f"""BOOPPA COMPLIANCE AUDIT REPORT - EXECUTIVE SUMMARY

Overall Risk Level: {risk_level['level']} ({risk_level['description']})

This audit identified {total_count} compliance issues requiring attention:
• CRITICAL violations: {critical_count} (require immediate action within 24-48 hours)
• HIGH severity violations: {high_count} (require urgent attention within 7 days)
• MEDIUM/LOW violations: {total_count - critical_count - high_count} (address within 30 days)

KEY FINDINGS:
"""

        # Add key findings for critical violations
        for violation in violations:
            if violation.get("severity") in ["CRITICAL", "HIGH"]:
                summary += f"• {violation.get('type', 'Violation').replace('_', ' ').title()}: {violation.get('details', '')}\n"

        summary += f"""

RECOMMENDED IMMEDIATE ACTIONS:
1. Address all CRITICAL violations within 24-48 hours
2. Develop compliance action plan with clear deadlines
3. Document all corrective measures with blockchain timestamps
4. Schedule follow-up audit in 30 days

BLOCKCHAIN EVIDENCE:
All compliance actions should be documented on Polygon blockchain for court-admissible evidence.
Transaction hashes should be stored in compliance records.

NEXT STEPS:
Review detailed findings section for specific violations and remediation steps.
Consult legal counsel for interpretation of regulatory requirements."""

        return summary

    def _generate_recommendations(self, violations: List[Dict]) -> List[Dict]:
        """Generate specific recommendations based on violations"""
        recommendations = []

        for violation in violations:
            severity = violation.get("severity", "MEDIUM")
            v_type = violation.get("type", "")

            rec = {
                "violation_type": v_type,
                "severity": severity,
                "priority": "HIGH" if severity in ["CRITICAL", "HIGH"] else "MEDIUM",
                "actions": [],
                "timeline": get_compliance_deadline(severity),
            }

            # Add type-specific recommendations
            if v_type == "nric_violation":
                rec["actions"] = [
                    "Remove NRIC collection forms immediately",
                    "Implement alternative identification methods",
                    "Add legal justification if NRIC collection is required",
                    "Update privacy policy to reflect changes",
                ]
            elif v_type == "security_violation":
                rec["actions"] = [
                    "Deploy HTTPS certificate immediately",
                    "Configure security headers (HSTS, CSP)",
                    "Conduct security vulnerability assessment",
                    "Implement Web Application Firewall",
                ]
            elif v_type == "cookie_violation":
                rec["actions"] = [
                    "Implement compliant cookie consent banner",
                    "Ensure active opt-in (no pre-ticked boxes)",
                    "Provide granular consent options",
                    "Include multi-language support",
                ]
            else:
                rec["actions"] = [
                    "Review compliance requirements",
                    "Consult relevant guidelines",
                    "Implement corrective measures",
                    "Document compliance actions",
                ]

            recommendations.append(rec)

        return recommendations

    def _generate_blockchain_instructions(
        self, scan_data: Dict, violations: List[Dict]
    ) -> Dict:
        """Generate blockchain evidence instructions"""
        return {
            "purpose": "Create court-admissible evidence of compliance actions",
            "blockchain": "Polygon Mainnet (Proof-of-Stake)",
            "steps": [
                "1. Generate SHA-256 hash of compliance action documentation",
                "2. Submit hash to Booppa EvidenceAnchor smart contract",
                "3. Wait for transaction confirmation (2-3 minutes)",
                "4. Store transaction hash in compliance records",
                "5. Include Polygonscan QR code in audit reports",
            ],
            "verification": {
                "url_format": "https://polygonscan.com/tx/{transaction_hash}",
                "qr_code": "Generate QR code for mobile verification",
                "court_admissibility": "Recognized under Singapore Evidence Act",
                "timestamp_proof": "Immutable timestamp on blockchain",
            },
            "cost_estimate": "S$0.01 - S$0.05 per transaction",
            "recommended_actions": [
                f"Anchor initial audit report: BOOPPA-{datetime.now().strftime('%Y%m%d')}",
                "Anchor each major compliance milestone",
                "Anchor final compliance confirmation",
            ],
        }

    def _get_risk_breakdown(self, violations: List[Dict]) -> Dict:
        """Get detailed risk breakdown"""
        breakdown = {"critical": 0, "high": 0, "medium": 0, "low": 0, "by_type": {}}

        for violation in violations:
            severity = violation.get("severity", "MEDIUM").lower()
            v_type = violation.get("type", "unknown")

            if severity in breakdown:
                breakdown[severity] += 1

            if v_type not in breakdown["by_type"]:
                breakdown["by_type"][v_type] = 0
            breakdown["by_type"][v_type] += 1

        return breakdown

    def _get_relevant_references(self, violations: List[Dict]) -> List[Dict]:
        """Get relevant legal references based on violations"""
        references = []
        violation_types = set(v.get("type") for v in violations)

        # Always include PDPA reference
        references.append(
            {
                "title": "Personal Data Protection Act 2012",
                "url": "https://sso.agc.gov.sg/Act/PDPA2012",
                "relevance": "Core legislation for all data protection in Singapore",
            }
        )

        # Add specific references based on violations
        if any("nric" in vt for vt in violation_types):
            references.append(SINGAPORE_LEGISLATION["PDPC_ADVISORIES"]["nric_2018"])

        if any("cookie" in vt for vt in violation_types):
            references.append(SINGAPORE_LEGISLATION["PDPC_ADVISORIES"]["cookies_2021"])

        if any("security" in vt for vt in violation_types):
            references.append(
                {
                    "title": "Cybersecurity Act 2018",
                    "url": "https://sso.agc.gov.sg/Act/CA2018",
                    "relevance": "Framework for cybersecurity in Singapore",
                }
            )

        return references

    def _generate_next_steps(self, violations: List[Dict]) -> List[str]:
        """Generate next steps for the company"""
        steps = [
            "1. Review this report with legal counsel",
            "2. Prioritize CRITICAL and HIGH severity violations",
            "3. Develop compliance action plan with deadlines",
            "4. Implement corrective measures",
            "5. Document all actions with blockchain timestamps",
            "6. Schedule follow-up audit in 30 days",
            "7. Update compliance documentation and policies",
            "8. Train staff on compliance requirements",
        ]

        # Add specific steps based on violations
        if any(v.get("type") == "nric_violation" for v in violations):
            steps.append(
                "9. Review all data collection points for unnecessary personal data"
            )

        if any(v.get("type") == "security_violation" for v in violations):
            steps.append("10. Conduct comprehensive security assessment")

        return steps

    def _get_violation_legislation(self, violation_type: str) -> List[str]:
        """Get legislation references for violation type"""
        legislation_map = {
            "nric_violation": ["PDPA Section 18", "PDPC Advisory Guidelines 2018"],
            "cookie_violation": [
                "PDPA Section 13",
                "PDPC Guide to Enhanced Notice 2021",
            ],
            "security_violation": [
                "PDPA Section 24",
                "Cybersecurity Act 2018",
                "MAS Notice 644",
            ],
            "organizational_violation": [
                "PDPA Section 11",
                "PDPC Guide to Accountability",
            ],
            "marketing_violation": ["DNC Registry", "Spam Control Act"],
        }

        return legislation_map.get(violation_type, ["PDPA General Provisions"])

    def _get_priority_level(self, severity: str) -> str:
        """Get priority level for remediation"""
        priority_map = {
            "CRITICAL": "Immediate (24-48 hours)",
            "HIGH": "Urgent (7 days)",
            "MEDIUM": "Important (30 days)",
            "LOW": "Planning (60 days)",
        }
        return priority_map.get(severity, "Important (30 days)")

    def _get_disclaimer(self) -> str:
        """Get legal disclaimer for report"""
        return """LEGAL DISCLAIMER:
This automated compliance report is generated by Booppa AI based on automated scanning and AI analysis. 
It is intended for preliminary assessment purposes only and does not constitute legal advice. 
The accuracy of findings depends on the completeness of the scan and current regulatory interpretations. 
Organizations should consult qualified legal professionals for definitive compliance guidance and before 
taking any legal or regulatory actions. Booppa is not a law firm and does not provide legal services. 
Blockchain evidence admissibility is subject to judicial acceptance and proper implementation."""
