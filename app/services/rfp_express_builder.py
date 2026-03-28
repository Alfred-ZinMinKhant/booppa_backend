"""
RFP Kit Express Builder — SGD 129
==================================
Generates a 2-page evidence certificate for vendors responding to GeBIZ RFPs.

Flow:
  1. Derive vendor context from DB (company, UEN, sector, score)
  2. Generate 5 essential RFP Q&A answers via BooppaAIService
  3. Build PDF certificate via PDFService
  4. Upload PDF to S3 via S3Service
  5. Send delivery email via RFPExpressEmailer
  6. Return download URL + metadata

RFP Kit Complete (SGD 499) follows the same flow with 15 questions and
an editable DOCX — handled by a separate builder.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ── Question sets ─────────────────────────────────────────────────────────────
# Express (5 questions) — core GeBIZ requirements
ESSENTIAL_QUESTIONS = [
    "data_policy",       # PDPA / data handling policy
    "dpo_appointed",     # DPO appointment status
    "security_measures", # Technical/organisational security controls
    "breach_history",    # Incident history (last 24 months)
    "third_party",       # Third-party vendor / sub-processor management
]

# Complete (15 questions) — full procurement evidence pack
COMPLETE_QUESTIONS = ESSENTIAL_QUESTIONS + [
    "iso_certifications",   # ISO 27001 / SOC 2 status
    "business_continuity",  # BCP / DR plan
    "staff_training",       # Security awareness training
    "access_controls",      # IAM and privileged access management
    "vulnerability_mgmt",   # Patch management and vulnerability scanning
    "encryption_standards", # Encryption algorithms and key management
    "audit_logging",        # Audit log retention and monitoring
    "incident_response",    # Incident response plan and contact
    "data_residency",       # Where data is stored (Singapore / overseas)
    "subcontracting",       # Subcontracting / offshoring policy
]

QUESTION_LABELS: dict[str, str] = {
    "data_policy":          "Do you have a PDPA data protection policy?",
    "dpo_appointed":        "Has a Data Protection Officer (DPO) been appointed?",
    "security_measures":    "What security measures are in place to protect personal data?",
    "breach_history":       "Have there been any data breaches in the past 24 months?",
    "third_party":          "How do you manage third-party vendors who handle personal data?",
    "iso_certifications":   "Does your organisation hold ISO 27001, SOC 2, or equivalent certification?",
    "business_continuity":  "Do you have a Business Continuity / Disaster Recovery plan?",
    "staff_training":       "How do you train staff on data protection and cybersecurity?",
    "access_controls":      "Describe your Identity and Access Management (IAM) controls.",
    "vulnerability_mgmt":   "How do you manage software vulnerabilities and patching?",
    "encryption_standards": "What encryption standards do you use for data at rest and in transit?",
    "audit_logging":        "How long are audit logs retained and how are they monitored?",
    "incident_response":    "Describe your incident response process and escalation path.",
    "data_residency":       "Where is data stored — Singapore, or overseas? What cross-border safeguards apply?",
    "subcontracting":       "Do you subcontract or offshore any processing involving personal data?",
}


class RFPExpressBuilder:
    """Generate RFP Kit Express package for a vendor."""

    def __init__(self, vendor_id: str, vendor_email: str):
        self.vendor_id    = vendor_id
        self.vendor_email = vendor_email
        self.report_id    = str(uuid.uuid4())
        self.errors: list[str]    = []
        self.warnings: list[str]  = []
        self.generation_start     = datetime.utcnow()

    # ── Public entry point ────────────────────────────────────────────────────

    async def generate_express_package(
        self,
        vendor_url: str,
        company_name: str,
        rfp_details: Optional[Dict] = None,
        db=None,
        product_type: str = "rfp_express",
    ) -> Dict[str, Any]:
        logger.info(f"RFP Kit Express: starting for {company_name} ({vendor_url})")

        questions = COMPLETE_QUESTIONS if product_type == "rfp_complete" else ESSENTIAL_QUESTIONS

        # 1. Gather vendor context for personalised answers
        vendor_ctx = self._build_vendor_context(company_name, vendor_url, db)

        # 2. Generate RFP Q&A answers via AI
        qa_answers = await self._generate_qa(vendor_ctx, rfp_details, questions)

        # 2.5. Anchor report ID to blockchain (Polygon Amoy testnet)
        tx_hash = await self._anchor_to_blockchain()

        # 3. Build PDF (embed tx_hash if available)
        pdf_bytes = self._build_pdf(company_name, vendor_url, qa_answers, vendor_ctx, tx_hash, product_type)

        # 4. Upload to S3
        download_url = await self._upload_pdf(pdf_bytes, product_type)

        # 5. Send email
        await self._send_email(company_name, download_url, product_type)

        elapsed = (datetime.utcnow() - self.generation_start).total_seconds()
        logger.info(f"RFP Kit Express complete in {elapsed:.1f}s for {company_name}")

        from app.core.config import settings
        explorer_base = settings.POLYGON_EXPLORER_URL.rstrip("/")

        is_complete = product_type == "rfp_complete"
        return {
            "success":        True,
            "product":        "rfp_kit_complete" if is_complete else "rfp_kit_express",
            "price":          "SGD 599" if is_complete else "SGD 249",
            "vendor_id":      self.vendor_id,
            "company_name":   company_name,
            "vendor_url":     vendor_url,
            "download_url":   download_url,
            "qa_answers_count": len(COMPLETE_QUESTIONS if is_complete else ESSENTIAL_QUESTIONS),
            "tx_hash":        tx_hash,
            "polygonscan_url": f"{explorer_base}/tx/{tx_hash}" if tx_hash else None,
            "network":        "Polygon Amoy Testnet",
            "testnet_notice": "Anchored on Polygon Amoy testnet. Not yet on mainnet.",
            "upsell_available": not is_complete,
            "upsell_product": None if is_complete else "rfp_kit_complete",
            "upsell_price":   None if is_complete else "SGD 599",
            "errors":         self.errors,
            "warnings":       self.warnings,
            "generated_at":   self.generation_start.isoformat(),
            "generation_time_seconds": elapsed,
            "expires_at":     (datetime.utcnow() + timedelta(days=7)).isoformat(),
        }

    # ── Step 1: vendor context ────────────────────────────────────────────────

    def _build_vendor_context(self, company_name: str, vendor_url: str, db) -> Dict:
        ctx: Dict[str, Any] = {
            "company_name": company_name,
            "vendor_url":   vendor_url,
            "uen":          None,
            "sector":       None,
            "trust_score":  None,
            "verification_depth": None,
        }
        if db is None:
            return ctx
        try:
            from app.core.models import User, VendorScore
            from app.core.models_v6 import VendorSector
            user = db.query(User).filter(User.id == self.vendor_id).first()
            if user:
                ctx["uen"] = getattr(user, "uen", None)
            score = db.query(VendorScore).filter(VendorScore.vendor_id == self.vendor_id).first()
            if score:
                ctx["trust_score"] = score.total_score
            sector_row = db.query(VendorSector).filter(
                VendorSector.vendor_id == self.vendor_id
            ).first()
            if sector_row:
                ctx["sector"] = sector_row.sector
        except Exception as e:
            logger.warning(f"Could not fetch vendor context for {self.vendor_id}: {e}")
        return ctx

    # ── Step 2: AI-generated Q&A ──────────────────────────────────────────────

    async def _generate_qa(self, ctx: Dict, rfp_details: Optional[Dict], questions: list) -> Dict[str, str]:
        try:
            from app.services.booppa_ai_service import BooppaAIService
            ai = BooppaAIService()

            sector_hint = f" in the {ctx['sector']} sector" if ctx.get("sector") else ""
            rfp_hint    = f" The RFP is for: {rfp_details.get('description', '')}." if rfp_details else ""
            keys_list   = ", ".join(questions)

            prompt = (
                f"You are generating RFP compliance answers for {ctx['company_name']}"
                f"{sector_hint} (website: {ctx['vendor_url']}).{rfp_hint}\n\n"
                f"Write concise, professional answers for a Singapore government procurement RFP. "
                f"Each answer should be 1-3 sentences. Return ONLY a JSON object with these keys:\n"
                f"{keys_list}.\n\n"
                f"Base the answers on what a well-run Singapore SME in this sector would truthfully state."
            )

            response = await ai._call_deepseek([{"role": "user", "content": prompt}])

            import json, re
            # Extract JSON block from AI response
            match = re.search(r'\{.*\}', response, re.DOTALL)
            if match:
                return json.loads(match.group())
        except Exception as e:
            logger.warning(f"AI Q&A generation failed, using template fallback: {e}")
            self.warnings.append("AI Q&A used template fallback")

        # Fallback: template answers
        return self._template_qa(ctx, questions)

    def _template_qa(self, ctx: Dict, questions: list) -> Dict[str, str]:
        name = ctx["company_name"]
        url  = ctx["vendor_url"]
        all_answers = {
            "data_policy":          f"{name} maintains a PDPA-compliant Personal Data Protection Policy, accessible at {url}. All personal data is collected with consent and retained only for its stated purpose.",
            "dpo_appointed":        f"{name} has appointed a Data Protection Officer (DPO) responsible for overseeing data protection compliance and serving as the point of contact for data-related inquiries.",
            "security_measures":    f"{name} implements encryption at rest and in transit, role-based access controls, multi-factor authentication for privileged accounts, and conducts quarterly security reviews.",
            "breach_history":       f"{name} has not experienced any notifiable data breaches in the past 24 months. An incident response plan is in place and tested annually.",
            "third_party":          f"{name} conducts due diligence assessments on all third-party vendors and requires Data Processing Agreements (DPAs) before any personal data is shared with sub-processors.",
            "iso_certifications":   f"{name} is currently pursuing ISO 27001 certification and maintains internal controls aligned with the standard. SOC 2 readiness assessment is planned for the next financial year.",
            "business_continuity":  f"{name} maintains a Business Continuity Plan (BCP) and Disaster Recovery (DR) plan, reviewed annually. Critical systems have RTO of 4 hours and RPO of 24 hours.",
            "staff_training":       f"{name} conducts mandatory annual data protection and cybersecurity awareness training for all staff. New hires complete training within the first 30 days of employment.",
            "access_controls":      f"{name} enforces role-based access control (RBAC) with least-privilege principles. Privileged access is subject to MFA, quarterly reviews, and immediate revocation upon role change.",
            "vulnerability_mgmt":   f"{name} applies security patches within 30 days of release for critical vulnerabilities. Monthly vulnerability scans are conducted and remediation tracked to closure.",
            "encryption_standards": f"{name} uses AES-256 for data at rest and TLS 1.2+ for data in transit. Encryption keys are managed through a dedicated key management process with annual rotation.",
            "audit_logging":        f"{name} retains audit logs for a minimum of 12 months. Logs are centralised, monitored for anomalies, and protected from tampering.",
            "incident_response":    f"{name} maintains a documented Incident Response Plan with defined escalation paths. The DPO is notified within 24 hours of a suspected breach; PDPC notification is made within 3 business days if required.",
            "data_residency":       f"{name} stores all personal data on servers located in Singapore. Any cross-border transfers are governed by contractual clauses consistent with PDPA's Third Schedule requirements.",
            "subcontracting":       f"{name} does not offshore personal data processing. Any subcontracting engagements require prior written approval and binding data processing agreements.",
        }
        return {k: all_answers[k] for k in questions if k in all_answers}

    # ── Step 2.5: blockchain anchor ───────────────────────────────────────────

    async def _anchor_to_blockchain(self) -> Optional[str]:
        try:
            from app.services.blockchain import BlockchainService
            blockchain = BlockchainService()
            tx = await blockchain.anchor_evidence(
                self.report_id,
                metadata=f"rfp_express:vendor:{self.vendor_id}",
            )
            logger.info(f"RFP Express anchored on Polygon Amoy testnet: {tx}")
            return tx
        except Exception as e:
            logger.warning(f"Blockchain anchor failed for RFP Express (non-blocking): {e}")
            self.warnings.append(f"Blockchain anchor skipped: {e}")
            return None

    # ── Step 3: build PDF ─────────────────────────────────────────────────────

    def _build_pdf(
        self,
        company_name: str,
        vendor_url: str,
        qa_answers: Dict[str, str],
        ctx: Dict,
        tx_hash: Optional[str] = None,
        product_type: str = "rfp_express",
    ) -> bytes:
        try:
            from app.services.pdf_service import PDFService
            from app.core.config import settings
            pdf = PDFService()
            verify_base = settings.VERIFY_BASE_URL.rstrip("/")

            qa_section = "\n\n".join(
                f"Q: {self._q_label(k)}\nA: {v}"
                for k, v in qa_answers.items()
            )

            explorer_base = settings.POLYGON_EXPLORER_URL.rstrip("/")
            blockchain_info = (
                f"Blockchain TX: {tx_hash}\n"
                f"Network: Polygon Amoy Testnet\n"
                f"Note: Anchored on Polygon Amoy testnet. Not yet on mainnet.\n"
                f"Verify: {explorer_base}/tx/{tx_hash}"
            ) if tx_hash else "Blockchain anchor pending."

            is_complete = product_type == "rfp_complete"
            framework_label = "RFP Kit Complete Evidence Pack" if is_complete else "RFP Kit Express Evidence Certificate"
            report_data = {
                "company_name": company_name,
                "created_at":   datetime.utcnow().strftime("%d %b %Y %H:%M UTC"),
                "framework":    framework_label,
                "product_type": product_type,
                "summary":      (
                    f"This certificate confirms that {company_name} has completed the "
                    f"BOOPPA {'RFP Kit Complete' if is_complete else 'RFP Kit Express'} process, "
                    f"generating blockchain-anchored evidence for procurement submission."
                ),
                "key_issues":   [],
                "recommendations": [
                    f"Vendor URL: {vendor_url}",
                    f"Report ID: {self.report_id}",
                    f"Sector: {ctx.get('sector') or 'General'}",
                    f"UEN: {ctx.get('uen') or 'Not provided'}",
                    blockchain_info,
                ],
                "qa_section": qa_section,
                "audit_hash": self.report_id,
                "verify_url": f"{verify_base}/verify/{self.report_id}",
                "tx_hash": tx_hash,
            }
            return pdf.generate_pdf(report_data)
        except Exception as e:
            logger.error(f"PDF generation failed: {e}")
            self.errors.append(f"PDF generation error: {e}")
            raise

    def _q_label(self, key: str) -> str:
        return QUESTION_LABELS.get(key, key.replace("_", " ").title())

    # ── Step 4: upload to S3 ──────────────────────────────────────────────────

    async def _upload_pdf(self, pdf_bytes: bytes, product_type: str = "rfp_express") -> str:
        try:
            from app.services.storage import S3Service
            s3 = S3Service()
            folder = "rfp-complete" if product_type == "rfp_complete" else "rfp-express"
            url = await s3.upload_pdf(pdf_bytes, f"{folder}/{self.report_id}")
            return url
        except Exception as e:
            logger.error(f"S3 upload failed: {e}")
            self.errors.append(f"Upload error: {e}")
            raise

    # ── Step 5: email ─────────────────────────────────────────────────────────

    async def _send_email(self, company_name: str, download_url: str, product_type: str = "rfp_express"):
        try:
            from app.services.rfp_express_emailer import RFPExpressEmailer
            emailer = RFPExpressEmailer()
            await emailer.send_express_ready_email(
                customer_email=self.vendor_email,
                vendor_name=company_name,
                download_url=download_url,
                product_type=product_type,
            )
        except Exception as e:
            logger.warning(f"Email delivery failed (non-blocking): {e}")
            self.warnings.append(f"Email not sent: {e}")
