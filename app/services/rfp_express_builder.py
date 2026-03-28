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

# ── Essential question keys ───────────────────────────────────────────────────
ESSENTIAL_QUESTIONS = [
    "data_policy",       # PDPA / data handling policy
    "dpo_appointed",     # DPO appointment status
    "security_measures", # Technical/organisational security controls
    "breach_history",    # Incident history (last 24 months)
    "third_party",       # Third-party vendor / sub-processor management
]


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
    ) -> Dict[str, Any]:
        logger.info(f"RFP Kit Express: starting for {company_name} ({vendor_url})")

        # 1. Gather vendor context for personalised answers
        vendor_ctx = self._build_vendor_context(company_name, vendor_url, db)

        # 2. Generate 5 RFP Q&A answers via AI
        qa_answers = await self._generate_qa(vendor_ctx, rfp_details)

        # 2.5. Anchor report ID to blockchain (Polygon Amoy testnet)
        tx_hash = await self._anchor_to_blockchain()

        # 3. Build PDF (embed tx_hash if available)
        pdf_bytes = self._build_pdf(company_name, vendor_url, qa_answers, vendor_ctx, tx_hash)

        # 4. Upload to S3
        download_url = await self._upload_pdf(pdf_bytes)

        # 5. Send email
        await self._send_email(company_name, download_url)

        elapsed = (datetime.utcnow() - self.generation_start).total_seconds()
        logger.info(f"RFP Kit Express complete in {elapsed:.1f}s for {company_name}")

        from app.core.config import settings
        explorer_base = settings.POLYGON_EXPLORER_URL.rstrip("/")

        return {
            "success":        True,
            "product":        "rfp_kit_express",
            "price":          "SGD 129",
            "vendor_id":      self.vendor_id,
            "company_name":   company_name,
            "vendor_url":     vendor_url,
            "download_url":   download_url,
            "qa_answers_count": len(ESSENTIAL_QUESTIONS),
            "tx_hash":        tx_hash,
            "polygonscan_url": f"{explorer_base}/tx/{tx_hash}" if tx_hash else None,
            "network":        "Polygon Amoy Testnet",
            "testnet_notice": "Anchored on Polygon Amoy testnet. Not yet on mainnet.",
            "upsell_available": True,
            "upsell_product": "rfp_kit_complete",
            "upsell_price":   "SGD 499",
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

    async def _generate_qa(self, ctx: Dict, rfp_details: Optional[Dict]) -> Dict[str, str]:
        try:
            from app.services.booppa_ai_service import BooppaAIService
            ai = BooppaAIService()

            sector_hint = f" in the {ctx['sector']} sector" if ctx.get("sector") else ""
            rfp_hint    = f" The RFP is for: {rfp_details.get('description', '')}." if rfp_details else ""

            prompt = (
                f"You are generating RFP compliance answers for {ctx['company_name']}"
                f"{sector_hint} (website: {ctx['vendor_url']}).{rfp_hint}\n\n"
                f"Write concise, professional answers for a Singapore government procurement RFP. "
                f"Each answer should be 1-3 sentences. Return ONLY a JSON object with these keys:\n"
                f"data_policy, dpo_appointed, security_measures, breach_history, third_party.\n\n"
                f"Base the answers on what a well-run Singapore SME in this sector would truthfully state."
            )

            response = await ai.analyze(prompt)

            import json, re
            # Extract JSON block from AI response
            match = re.search(r'\{.*\}', response, re.DOTALL)
            if match:
                return json.loads(match.group())
        except Exception as e:
            logger.warning(f"AI Q&A generation failed, using template fallback: {e}")
            self.warnings.append("AI Q&A used template fallback")

        # Fallback: template answers
        return self._template_qa(ctx)

    def _template_qa(self, ctx: Dict) -> Dict[str, str]:
        name = ctx["company_name"]
        url  = ctx["vendor_url"]
        return {
            "data_policy":       f"{name} maintains a PDPA-compliant Personal Data Protection Policy, accessible at {url}. All personal data is collected with consent and retained only for its stated purpose.",
            "dpo_appointed":     f"{name} has appointed a Data Protection Officer (DPO) responsible for overseeing data protection compliance and serving as the point of contact for data-related inquiries.",
            "security_measures": f"{name} implements encryption at rest and in transit, role-based access controls, multi-factor authentication for privileged accounts, and conducts quarterly security reviews.",
            "breach_history":    f"{name} has not experienced any notifiable data breaches in the past 24 months. An incident response plan is in place and tested annually.",
            "third_party":       f"{name} conducts due diligence assessments on all third-party vendors and requires Data Processing Agreements (DPAs) before any personal data is shared with sub-processors.",
        }

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
    ) -> bytes:
        try:
            from app.services.pdf_service import PDFService
            from app.core.config import settings
            pdf = PDFService()

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

            report_data = {
                "company_name": company_name,
                "created_at":   datetime.utcnow().strftime("%d %b %Y %H:%M UTC"),
                "framework":    "RFP Kit Express Evidence Certificate",
                "product_type": "rfp_express",
                "summary":      (
                    f"This certificate confirms that {company_name} has completed the "
                    f"BOOPPA RFP Kit Express process, generating blockchain-anchored "
                    f"evidence for procurement submission."
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
                "verify_url": f"https://booppa.io/verify/{self.report_id}",
                "tx_hash": tx_hash,
            }
            return pdf.generate_pdf(report_data)
        except Exception as e:
            logger.error(f"PDF generation failed: {e}")
            self.errors.append(f"PDF generation error: {e}")
            raise

    def _q_label(self, key: str) -> str:
        labels = {
            "data_policy":       "Do you have a PDPA data protection policy?",
            "dpo_appointed":     "Has a Data Protection Officer (DPO) been appointed?",
            "security_measures": "What security measures are in place to protect personal data?",
            "breach_history":    "Have there been any data breaches in the past 24 months?",
            "third_party":       "How do you manage third-party vendors who handle personal data?",
        }
        return labels.get(key, key.replace("_", " ").title())

    # ── Step 4: upload to S3 ──────────────────────────────────────────────────

    async def _upload_pdf(self, pdf_bytes: bytes) -> str:
        try:
            from app.services.storage import S3Service
            s3 = S3Service()
            url = await s3.upload_pdf(pdf_bytes, f"rfp-express/{self.report_id}")
            return url
        except Exception as e:
            logger.error(f"S3 upload failed: {e}")
            self.errors.append(f"Upload error: {e}")
            raise

    # ── Step 5: email ─────────────────────────────────────────────────────────

    async def _send_email(self, company_name: str, download_url: str):
        try:
            from app.services.rfp_express_emailer import RFPExpressEmailer
            emailer = RFPExpressEmailer()
            await emailer.send_express_ready_email(
                customer_email=self.vendor_email,
                vendor_name=company_name,
                download_url=download_url,
            )
        except Exception as e:
            logger.warning(f"Email delivery failed (non-blocking): {e}")
            self.warnings.append(f"Email not sent: {e}")
