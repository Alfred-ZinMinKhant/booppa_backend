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

    def __init__(self, vendor_id: str, vendor_email: str, session_id: str | None = None):
        self.vendor_id    = vendor_id
        self.vendor_email = vendor_email
        self.session_id   = session_id
        # 4.12: Idempotent report_id — same session always produces the same ID (safe retries)
        if session_id:
            self.report_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"rfp:{session_id}"))
        else:
            self.report_id = str(uuid.uuid4())
        self.errors: list[str]    = []
        self.warnings: list[str]  = []
        self.used_template: bool  = False  # 4.3: track if AI failed and template was used
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

        intake = (rfp_details or {}).get("intake", {})

        # 1. Gather vendor context (DB: UEN, sector, trust score, ACRA local match)
        vendor_ctx = self._build_vendor_context(company_name, vendor_url, db, intake=intake)

        # 1b. External evidence enrichment (parallel async calls)
        from app.services.evidence_enricher import (
            fetch_acra_status, fetch_pdpc_enforcement,
            fetch_ssl_grade, fetch_domain_reputation, fetch_hosting_signals, check_consistency,
        )
        import asyncio as _asyncio
        uen = vendor_ctx.get("uen") or intake.get("uen")
        async def _no_acra() -> dict:
            return {"found": False}

        stated_hosting = intake.get("data_hosting") or intake.get("primary_cloud")
        (acra_live, pdpc_result, ssl_result, domain_rep, hosting_signals) = await _asyncio.gather(
            fetch_acra_status(uen) if uen else _no_acra(),
            fetch_pdpc_enforcement(company_name, uen),
            fetch_ssl_grade(vendor_url),
            fetch_domain_reputation(vendor_url),
            fetch_hosting_signals(vendor_url, stated_hosting=stated_hosting),
        )

        # Merge live ACRA data into vendor context
        if acra_live.get("found"):
            if not vendor_ctx.get("acra_name"):
                vendor_ctx["acra_name"] = acra_live.get("registered_name")
            if not vendor_ctx.get("acra_entity_type"):
                vendor_ctx["acra_entity_type"] = acra_live.get("entity_type")
            vendor_ctx["acra_live"] = acra_live.get("live", True)
            vendor_ctx["acra_status"] = acra_live.get("entity_status")
            if acra_live.get("warning"):
                self.warnings.append(f"[ACRA] {acra_live['warning']}")
        if pdpc_result.get("warning"):
            self.warnings.append(f"[PDPC] {pdpc_result['warning']}")
        if ssl_result.get("warning"):
            self.warnings.append(f"[SSL] {ssl_result['warning']}")
        if domain_rep.get("warning"):
            self.warnings.append(f"[VT] {domain_rep['warning']}")
        if hosting_signals.get("mismatch_warning"):
            self.warnings.append(f"[Hosting] {hosting_signals['mismatch_warning']}")
        # Surface inferred provider in AI context
        if hosting_signals.get("inferred_provider"):
            vendor_ctx["inferred_hosting_provider"] = hosting_signals["inferred_provider"]
            vendor_ctx["inferred_hosting_region"] = hosting_signals.get("inferred_region")

        # 4.4: Consistency check + website scrape (single fetch, reused below)
        ws_data = await self._fetch_website_context(vendor_url)
        website_text = ws_data.get("text", "") if isinstance(ws_data, dict) else (ws_data or "")
        # Surface privacy policy URL in vendor_ctx for PDF/email
        if isinstance(ws_data, dict) and ws_data.get("privacy_policy_url"):
            vendor_ctx.setdefault("privacy_policy_url", ws_data["privacy_policy_url"])
        # SPA warning
        if isinstance(ws_data, dict) and ws_data.get("spa_warning") and ws_data["spa_warning"] not in self.warnings:
            self.warnings.append(f"[SPA] {ws_data['spa_warning']}")

        # 2. Generate RFP Q&A answers via AI (enriched prompt) — pass ws_data so no re-fetch
        qa_answers = await self._generate_qa(
            vendor_ctx, rfp_details, questions,
            ssl_result=ssl_result, domain_rep=domain_rep,
            ws_data=ws_data,
        )

        # Consistency check — intake vs external evidence
        discrepancies = check_consistency(intake, website_text, pdpc_result, domain_rep)
        if discrepancies:
            self.warnings.extend([f"[Discrepancy] {d}" for d in discrepancies])

        # Tag which answers are grounded in real facts vs AI-assumed
        fact_backed_keys = self._fact_backed_keys(intake, vendor_ctx, ssl_result, domain_rep, acra_live)

        # 2.5. Anchor report ID to blockchain
        tx_hash = await self._anchor_to_blockchain()

        # 3. Build PDF
        pdf_bytes = self._build_pdf(
            company_name, vendor_url, qa_answers, vendor_ctx, tx_hash, product_type,
            acra_live=acra_live, pdpc_result=pdpc_result, discrepancies=discrepancies,
            intake=intake,
        )

        # 4. Upload to S3
        download_url = await self._upload_pdf(pdf_bytes, product_type)

        # 4b. For Complete tier, also generate and upload DOCX
        docx_url = None
        if product_type == "rfp_complete":
            docx_bytes = self._build_docx(company_name, vendor_url, qa_answers, vendor_ctx, tx_hash, product_type, intake=intake)
            if docx_bytes:
                docx_url = await self._upload_docx(docx_bytes)

        # 4c. Write CertificateLog audit row (4.11)
        await self._write_certificate_log(pdf_bytes, download_url, db)

        # 5. Send email
        await self._send_email(company_name, download_url, product_type, docx_url=docx_url)

        elapsed = (datetime.utcnow() - self.generation_start).total_seconds()
        logger.info(f"RFP Kit Express complete in {elapsed:.1f}s for {company_name}")

        from app.core.config import settings
        explorer_base = settings.POLYGON_EXPLORER_URL.rstrip("/")

        is_complete = product_type == "rfp_complete"
        # Build labelled Q&A for frontend display, with confidence tagging
        qa_display = [
            {
                "question": self._q_label(k),
                "answer": v,
                "confidence": "fact" if k in fact_backed_keys else "generated",
            }
            for k, v in qa_answers.items()
        ]

        return {
            "success":        True,
            "product":        "rfp_kit_complete" if is_complete else "rfp_kit_express",
            "price":          "SGD 599" if is_complete else "SGD 249",
            "vendor_id":      self.vendor_id,
            "company_name":   company_name,
            "vendor_url":     vendor_url,
            "download_url":   download_url,
            "docx_url":       docx_url,
            "qa_answers":     qa_display,
            "qa_answers_count": len(qa_display),
            "tx_hash":        tx_hash,
            "polygonscan_url": f"{explorer_base}/tx/{tx_hash}" if tx_hash else None,
            "network":        "Polygon Amoy Testnet",
            "testnet_notice": "Anchored on Polygon Amoy testnet. Not yet on mainnet.",
            "upsell_available": not is_complete,
            "upsell_product": None if is_complete else "rfp_kit_complete",
            "upsell_price":   None if is_complete else "SGD 599",
            "errors":         self.errors,
            "warnings":       self.warnings,
            "answer_source":  "template" if self.used_template else "ai_grounded",
            "discrepancies":  discrepancies,
            "data_sources": {
                "acra_verified":         acra_live.get("found", False),
                "acra_live":             acra_live.get("live"),
                "pdpc_checked":          pdpc_result.get("checked", False),
                "pdpc_flagged":          pdpc_result.get("found", False),
                "ssl_checked":           ssl_result.get("checked", False),
                "ssl_grade":             ssl_result.get("grade"),
                "vt_checked":            domain_rep.get("checked", False),
                "vt_flagged":            domain_rep.get("flagged", False),
                "vt_reputation":         domain_rep.get("reputation"),
                "hosting_checked":       hosting_signals.get("checked", False),
                "inferred_provider":     hosting_signals.get("inferred_provider"),
                "inferred_region":       hosting_signals.get("inferred_region"),
                "hosting_mismatch":      bool(hosting_signals.get("mismatch_warning")),
                "website_scraped":       bool(website_text),
                "privacy_policy_found":  bool(vendor_ctx.get("privacy_policy_url")),
                "gebiz_supplier":        vendor_ctx.get("gebiz_supplier", False),
                "gebiz_contracts":       vendor_ctx.get("gebiz_contracts_count", 0),
                "intake_supplied":       bool(intake),
                "ai_grounded":           not self.used_template,
            },
            "generated_at":   self.generation_start.isoformat(),
            "generation_time_seconds": elapsed,
            "expires_at":     (datetime.utcnow() + timedelta(days=7)).isoformat(),
        }

    # ── Step 1: vendor context ────────────────────────────────────────────────

    def _build_vendor_context(self, company_name: str, vendor_url: str, db, intake: dict | None = None) -> Dict:
        ctx: Dict[str, Any] = {
            "company_name": company_name,
            "vendor_url":   vendor_url,
            "uen":          intake.get("uen") if intake else None,
            "sector":       None,
            "trust_score":  None,
            "acra_name":    None,
            "acra_entity_type": None,
            "verification_depth": None,
            "gebiz_supplier": False,
            "gebiz_contracts_count": 0,
            "privacy_policy_url": None,
        }
        if db is None:
            return ctx
        try:
            from app.core.models import User, VendorScore
            from app.core.models_v6 import VendorSector
            import re as _re
            # vendor_id may be an email (for anonymous/no-account purchases) or a UUID
            if _re.match(r'^[0-9a-f-]{36}$', self.vendor_id or '', _re.IGNORECASE):
                user = db.query(User).filter(User.id == self.vendor_id).first()
            else:
                user = db.query(User).filter(User.email == self.vendor_id).first()
            if user:
                ctx["uen"] = getattr(user, "uen", None)
            # VendorScore / VendorSector are keyed by UUID — skip if vendor_id is an email
            is_uuid = _re.match(r'^[0-9a-f-]{36}$', self.vendor_id or '', _re.IGNORECASE)
            if is_uuid:
                score = db.query(VendorScore).filter(VendorScore.vendor_id == self.vendor_id).first()
                if score:
                    ctx["trust_score"] = score.total_score
            sector_row = db.query(VendorSector).filter(
                VendorSector.vendor_id == (user.id if user else self.vendor_id)
            ).first() if is_uuid or user else None
            if sector_row:
                ctx["sector"] = sector_row.sector

            # ACRA lookup — enrich with registered company name and entity type
            uen_to_check = ctx.get("uen") or (intake.get("uen") if intake else None)
            if uen_to_check:
                try:
                    from app.core.models_v10 import MarketplaceVendor
                    acra_row = db.query(MarketplaceVendor).filter(
                        MarketplaceVendor.uen == uen_to_check
                    ).first()
                    if acra_row:
                        ctx["acra_name"] = getattr(acra_row, "name", None) or getattr(acra_row, "company_name", None)
                        ctx["acra_entity_type"] = getattr(acra_row, "entity_type", None)
                        if not ctx.get("sector"):
                            ctx["sector"] = getattr(acra_row, "sector", None)
                        logger.info(f"ACRA match for UEN {uen_to_check}: {ctx['acra_name']}")
                except Exception as acra_err:
                    logger.warning(f"ACRA lookup failed for UEN {uen_to_check}: {acra_err}")

            # Audit fix D: GeBIZ supplier history
            try:
                from app.core.models_v10 import DiscoveredVendor
                disc = db.query(DiscoveredVendor).filter(
                    DiscoveredVendor.uen == uen_to_check
                ).first()
                if disc:
                    ctx["gebiz_supplier"] = bool(getattr(disc, "gebiz_supplier", False))
                    ctx["gebiz_contracts_count"] = getattr(disc, "gebiz_contracts_count", 0) or 0
                    logger.info(
                        f"GeBIZ supplier found for UEN {uen_to_check}: "
                        f"supplier={ctx['gebiz_supplier']} contracts={ctx['gebiz_contracts_count']}"
                    )
            except Exception as gebiz_err:
                logger.warning(f"GeBIZ supplier lookup failed for UEN {uen_to_check}: {gebiz_err}")
        except Exception as e:
            logger.warning(f"Could not fetch vendor context for {self.vendor_id}: {e}")
        return ctx

    # ── Step 2: AI-generated Q&A ──────────────────────────────────────────────

    async def _fetch_website_context(self, vendor_url: str) -> dict:
        """Fetch and extract readable text from the vendor's website for AI grounding.
        Returns dict: {text, privacy_policy_url, is_spa, spa_warning}
        Results are cached in Redis for 24 hours to avoid re-scraping on retries."""
        import hashlib, httpx
        from urllib.parse import urlparse
        from app.core.cache import cache as cache_mod

        cache_key = cache_mod.cache_key(f"rfp_scrape_v2:{hashlib.md5(vendor_url.encode()).hexdigest()}")
        cached = cache_mod.get(cache_key)
        if cached and isinstance(cached, dict) and "text" in cached:
            return cached

        try:
            from bs4 import BeautifulSoup
        except ImportError:
            BeautifulSoup = None

        import re as _re
        texts: list[str] = []
        privacy_policy_url: str | None = None
        is_spa = False

        base = vendor_url.rstrip("/")
        parsed_base = urlparse(vendor_url)
        pages = [base, base + "/about", base + "/privacy-policy", base + "/privacy"]
        headers = {"User-Agent": "Mozilla/5.0 (compatible; BooppaBot/1.0)"}

        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            for page_url in pages:
                try:
                    resp = await client.get(page_url, headers=headers)
                    if resp.status_code != 200:
                        continue
                    raw_html = resp.text

                    # Audit fix E: SPA detection — thin visible text + script tags
                    stripped = _re.sub(r'<[^>]+>', ' ', raw_html)
                    stripped = _re.sub(r'\s+', ' ', stripped).strip()
                    if len(stripped) < 300 and '<script' in raw_html:
                        is_spa = True

                    if BeautifulSoup:
                        soup = BeautifulSoup(raw_html, "lxml")
                        # Remove boilerplate tags
                        for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
                            tag.decompose()
                        text = soup.get_text(separator=" ", strip=True)

                        # Audit fix C/F: extract privacy policy URL from links
                        if privacy_policy_url is None:
                            for a in soup.find_all("a", href=True):
                                href = a["href"]
                                if any(kw in href.lower() for kw in ["privacy", "pdpa", "data-protection"]):
                                    if href.startswith("http"):
                                        privacy_policy_url = href
                                    elif href.startswith("/"):
                                        privacy_policy_url = (
                                            f"{parsed_base.scheme}://{parsed_base.netloc}{href}"
                                        )
                                    if privacy_policy_url:
                                        break
                    else:
                        text = stripped

                    texts.append(text[:1500])
                except Exception:
                    pass

        full_text = "\n\n---\n\n".join(texts)[:4000] if texts else ""
        spa_warning = (
            "Website appears to be a JavaScript SPA — scraped content may be incomplete. "
            "AI answers will be less specific."
            if is_spa else None
        )
        result = {
            "text": full_text,
            "privacy_policy_url": privacy_policy_url,
            "is_spa": is_spa,
            "spa_warning": spa_warning,
        }
        if full_text:
            cache_mod.set(cache_key, result, ttl=86400)  # 24 h
        return result

    async def _generate_qa(
        self,
        ctx: Dict,
        rfp_details: Optional[Dict],
        questions: list,
        ssl_result: Dict | None = None,
        domain_rep: Dict | None = None,
        ws_data: dict | None = None,   # pre-fetched website context — avoids double scrape
    ) -> Dict[str, str]:
        try:
            from app.services.booppa_ai_service import BooppaAIService
            ai = BooppaAIService()

            sector_hint = f" in the {ctx['sector']} sector" if ctx.get("sector") else ""
            rfp_hint    = f" The RFP is for: {rfp_details.get('description', '')}." if rfp_details else ""
            keys_list   = ", ".join(questions)

            # Buyer-supplied facts ground the answers in reality
            intake = (rfp_details or {}).get("intake", {})
            intake_lines = []
            fact_map = {
                "uen":                "Company UEN (Singapore registration number)",
                "description":        "What the company does",
                "dpo_appointed":      "DPO appointed (yes/no/in-progress)",
                "dpo_name":           "DPO full name",
                "dpo_email":          "DPO contact email",
                "dpo_registration":   "PDPC DPO registration number",
                "iso_status":         "ISO 27001 / SOC 2 certification status",
                "iso_cert_number":    "ISO 27001 certificate number",
                "iso_cert_expiry":    "ISO 27001 certificate expiry date",
                "data_hosting":       "Where data is hosted",
                "primary_cloud":      "Primary cloud provider",
                "cloud_region":       "Cloud region where data is stored",
                "breach_history":     "Data breaches in last 24 months",
                "bcp_last_tested":    "Date BCP/DR plan was last tested",
                "training_frequency": "How often staff security training is conducted",
                "key_processors":     "Key third-party data processors",
                "extra_notes":        "Additional notes from the vendor",
            }
            for key, label in fact_map.items():
                val = intake.get(key)
                if val and val not in ("unknown", ""):
                    intake_lines.append(f"- {label}: {val}")
            if ctx.get("uen") and not intake.get("uen"):
                intake_lines.append(f"- Company UEN: {ctx['uen']}")
            if ctx.get("acra_name"):
                intake_lines.append(f"- ACRA registered name: {ctx['acra_name']}")
            if ctx.get("acra_entity_type"):
                intake_lines.append(f"- Entity type: {ctx['acra_entity_type']}")
            if ctx.get("gebiz_supplier"):
                count = ctx.get("gebiz_contracts_count") or ""
                count_str = f" with {count} prior government contracts" if count else ""
                intake_lines.append(f"- GeBIZ registered supplier{count_str}")
            if ctx.get("privacy_policy_url"):
                intake_lines.append(f"- Published privacy policy URL: {ctx['privacy_policy_url']}")
            if ctx.get("inferred_hosting_provider"):
                region_str = f" (region: {ctx['inferred_hosting_region']})" if ctx.get("inferred_hosting_region") else ""
                intake_lines.append(
                    f"- Inferred hosting provider: {ctx['inferred_hosting_provider']}{region_str}"
                )

            # Audit fix: warn when ISO claimed but cert number missing (not independently verifiable)
            if intake.get("iso_status") == "certified" and not intake.get("iso_cert_number"):
                self.warnings.append(
                    "ISO 27001 declared certified but no certificate number provided — "
                    "evaluators cannot independently verify. Add iso_cert_number to intake."
                )

            facts_section = (
                "Known facts about this company (use these to make answers specific and accurate):\n"
                + "\n".join(intake_lines)
                if intake_lines else ""
            )

            # Use pre-fetched website data if provided, otherwise fetch now (fallback)
            if ws_data is None:
                ws_data = await self._fetch_website_context(ctx["vendor_url"])
            _ws_data = ws_data
            website_text = _ws_data.get("text", "") if isinstance(_ws_data, dict) else (_ws_data or "")
            if isinstance(_ws_data, dict) and _ws_data.get("spa_warning"):
                if _ws_data["spa_warning"] not in self.warnings:
                    self.warnings.append(f"[SPA] {_ws_data['spa_warning']}")
            # Inject privacy policy URL into facts if not already provided
            if isinstance(_ws_data, dict) and _ws_data.get("privacy_policy_url") and not ctx.get("privacy_policy_url"):
                ctx["privacy_policy_url"] = _ws_data["privacy_policy_url"]
                intake_lines.append(f"- Published privacy policy URL: {ctx['privacy_policy_url']}")
                facts_section = (
                    "Known facts about this company (use these to make answers specific and accurate):\n"
                    + "\n".join(intake_lines)
                    if intake_lines else ""
                )
            website_section = (
                f"Website content extract (use to infer actual products/services/practices):\n{website_text}"
                if website_text else ""
            )

            # Inject SSL and VirusTotal real data as additional facts
            external_facts = []
            if ssl_result and ssl_result.get("checked") and ssl_result.get("grade"):
                protos = ", ".join(ssl_result.get("protocols", [])) or "unknown"
                external_facts.append(f"- SSL Labs grade: {ssl_result['grade']} (protocols: {protos})")
            if domain_rep and domain_rep.get("checked"):
                if domain_rep.get("flagged"):
                    malicious = domain_rep.get("malicious_votes", 0)
                    external_facts.append(
                        f"- VirusTotal domain check: flagged as malicious by {malicious} vendor(s) — "
                        "disclose proactively in security_measures answer"
                    )
                else:
                    external_facts.append("- VirusTotal domain check: no malicious flags detected")

            if external_facts:
                external_section = "Verified external data:\n" + "\n".join(external_facts)
                facts_section = "\n\n".join(filter(None, [facts_section, external_section]))

            context_block = "\n\n".join(filter(None, [facts_section, website_section]))

            prompt = (
                f"You are generating RFP compliance answers for {ctx['company_name']}"
                f"{sector_hint} (website: {ctx['vendor_url']}).{rfp_hint}\n\n"
                + (f"{context_block}\n\n" if context_block else "")
                + f"Write concise, professional answers for a Singapore government procurement RFP. "
                f"Each answer should be 1-3 sentences. Where specific facts above contradict a generic "
                f"assumption, use the facts. Return ONLY a JSON object with these keys:\n"
                f"{keys_list}."
            )

            response = await ai._call_deepseek([{"role": "user", "content": prompt}])

            import json, re
            # Extract JSON block from AI response
            if response and isinstance(response, str):
                match = re.search(r'\{.*\}', response, re.DOTALL)
                if match:
                    return json.loads(match.group())
        except Exception as e:
            logger.warning(f"AI Q&A generation failed, using template fallback: {e}")
            self.warnings.append("AI Q&A used template fallback")

        # 4.3: Fallback — mark so PDF and result carry a disclosure
        self.used_template = True
        return self._template_qa(ctx, questions)

    # Audit fix A: per-answer prefix so evaluators know answers are templated
    _TEMPLATE_PREFIX = "[Standard template — review before submission] "

    def _template_qa(self, ctx: Dict, questions: list) -> Dict[str, str]:
        name = ctx["company_name"]
        url  = ctx["vendor_url"]
        p    = self._TEMPLATE_PREFIX
        all_answers = {
            "data_policy":          f"{p}{name} maintains a PDPA-compliant Personal Data Protection Policy, accessible at {url}. All personal data is collected with consent and retained only for its stated purpose.",
            "dpo_appointed":        f"{p}{name} has appointed a Data Protection Officer (DPO) responsible for overseeing data protection compliance and serving as the point of contact for data-related inquiries.",
            "security_measures":    f"{p}{name} implements encryption at rest and in transit, role-based access controls, multi-factor authentication for privileged accounts, and conducts quarterly security reviews.",
            "breach_history":       f"{p}{name} has not experienced any notifiable data breaches in the past 24 months. An incident response plan is in place and tested annually.",
            "third_party":          f"{p}{name} conducts due diligence assessments on all third-party vendors and requires Data Processing Agreements (DPAs) before any personal data is shared with sub-processors.",
            "iso_certifications":   f"{p}{name} is currently pursuing ISO 27001 certification and maintains internal controls aligned with the standard. SOC 2 readiness assessment is planned for the next financial year.",
            "business_continuity":  f"{p}{name} maintains a Business Continuity Plan (BCP) and Disaster Recovery (DR) plan, reviewed annually. Critical systems have RTO of 4 hours and RPO of 24 hours.",
            "staff_training":       f"{p}{name} conducts mandatory annual data protection and cybersecurity awareness training for all staff. New hires complete training within the first 30 days of employment.",
            "access_controls":      f"{p}{name} enforces role-based access control (RBAC) with least-privilege principles. Privileged access is subject to MFA, quarterly reviews, and immediate revocation upon role change.",
            "vulnerability_mgmt":   f"{p}{name} applies security patches within 30 days of release for critical vulnerabilities. Monthly vulnerability scans are conducted and remediation tracked to closure.",
            "encryption_standards": f"{p}{name} uses AES-256 for data at rest and TLS 1.2+ for data in transit. Encryption keys are managed through a dedicated key management process with annual rotation.",
            "audit_logging":        f"{p}{name} retains audit logs for a minimum of 12 months. Logs are centralised, monitored for anomalies, and protected from tampering.",
            "incident_response":    f"{p}{name} maintains a documented Incident Response Plan with defined escalation paths. The DPO is notified within 24 hours of a suspected breach; PDPC notification is made within 3 business days if required.",
            "data_residency":       f"{p}{name} stores all personal data on servers located in Singapore. Any cross-border transfers are governed by contractual clauses consistent with PDPA's Third Schedule requirements.",
            "subcontracting":       f"{p}{name} does not offshore personal data processing. Any subcontracting engagements require prior written approval and binding data processing agreements.",
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
        acra_live: Dict | None = None,
        pdpc_result: Dict | None = None,
        discrepancies: list | None = None,
        intake: dict | None = None,
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
            # 4.3: Template disclaimer
            template_warning = (
                "\n⚠ NOTICE: These answers were generated from a standard template because "
                "AI generation was unavailable. They have not been independently verified "
                "against company-specific information. Review and customise before submission."
                if self.used_template else ""
            )

            intake = intake or {}

            # ACRA verification line
            acra_line = "ACRA Status: Not verified"
            if acra_live and acra_live.get("found"):
                status = acra_live.get("entity_status", "")
                acra_line = (
                    f"ACRA Status: {'✓ LIVE' if acra_live.get('live') else '⚠ ' + status} "
                    f"({acra_live.get('entity_type', '')})"
                )

            # PDPC enforcement warning
            pdpc_line = ""
            if pdpc_result and pdpc_result.get("found"):
                pdpc_line = "⚠ PDPC Enforcement: Previous enforcement action found — see warnings."

            # Audit fix D: GeBIZ supplier line
            gebiz_line = ""
            if ctx.get("gebiz_supplier"):
                count = ctx.get("gebiz_contracts_count") or 0
                gebiz_line = (
                    f"✓ GeBIZ Registered Supplier — {count} prior government contract(s)"
                    if count else "✓ GeBIZ Registered Supplier"
                )

            # DPO contact details (audit fix B)
            dpo_line = ""
            dpo_name = intake.get("dpo_name") or ""
            dpo_email = intake.get("dpo_email") or ""
            if dpo_name or dpo_email:
                dpo_parts = [x for x in [dpo_name, dpo_email] if x]
                dpo_line = f"DPO Contact: {', '.join(dpo_parts)}"

            # Privacy policy URL (audit fix C/F)
            privacy_line = ""
            if ctx.get("privacy_policy_url"):
                privacy_line = f"Privacy Policy: {ctx['privacy_policy_url']}"

            # ISO certification details
            iso_line = ""
            if intake.get("iso_cert_number"):
                expiry = intake.get("iso_cert_expiry", "")
                iso_line = (
                    f"ISO 27001 Cert: {intake['iso_cert_number']}"
                    + (f" (expires {expiry})" if expiry else "")
                    + "  [Verify at bsigroup.com/en-SG/validate-bsi-issued-certificates/]"
                )
            elif intake.get("iso_status") and intake["iso_status"].lower() not in ("no", "none", "pursuing", ""):
                # ISO claimed via status field but no cert number supplied
                iso_line = (
                    f"ISO Status: {intake['iso_status']}  "
                    f"[Certificate number not provided — buyers should request cert for independent verification]"
                )

            # Discrepancy notes
            disc_lines = ""
            if discrepancies:
                disc_lines = "\n⚠ Unresolved Discrepancies:\n" + "\n".join(f"  • {d}" for d in discrepancies)

            vendor_details = "\n".join(filter(None, [
                f"Vendor URL: {vendor_url}",
                f"Report ID: {self.report_id}",
                f"Sector: {ctx.get('sector') or 'General'}",
                f"UEN: {ctx.get('uen') or 'Not provided'}",
                acra_line,
                gebiz_line,
                dpo_line,
                privacy_line,
                iso_line,
                pdpc_line,
                blockchain_info,
                template_warning,
                disc_lines,
            ]))
            report_data = {
                "company_name": company_name,
                "created_at":   datetime.utcnow().strftime("%d %b %Y %H:%M UTC"),
                "framework":    framework_label,
                "product_type": product_type,
                "summary":      (
                    f"This certificate confirms that {company_name} has completed the "
                    f"BOOPPA {'RFP Kit Complete' if is_complete else 'RFP Kit Express'} process, "
                    f"generating blockchain-anchored evidence for procurement submission.\n\n"
                    f"{vendor_details}"
                ),
                "key_issues":   [],
                # Use ai_narrative so PDFService renders as plain paragraphs (not structured mode)
                "ai_narrative": qa_section,
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

    def _fact_backed_keys(self, intake: dict, ctx: dict,
                          ssl_result: Dict | None = None,
                          domain_rep: Dict | None = None,
                          acra_live: Dict | None = None) -> set:
        """Return the set of question keys that have a real fact grounding them."""
        _empty = (None, "unknown", "")
        backed = set()
        # DPO — backed if status provided or name/email supplied
        if intake.get("dpo_appointed") not in _empty:
            backed.add("dpo_appointed")
        if intake.get("dpo_name") not in _empty or intake.get("dpo_email") not in _empty:
            backed.add("dpo_appointed")  # named DPO is stronger backing
        # ISO — backed if status or cert number provided
        if intake.get("iso_status") not in _empty:
            backed.add("iso_certifications")
        if intake.get("iso_cert_number") not in _empty:
            backed.add("iso_certifications")
        # Data residency — backed if hosting, cloud provider, or region provided
        if intake.get("data_hosting") not in _empty:
            backed.add("data_residency")
        if intake.get("primary_cloud") not in _empty or intake.get("cloud_region") not in _empty:
            backed.add("data_residency")
        # Breach history — backed by declaration or external check
        if intake.get("breach_history") not in _empty or (domain_rep and domain_rep.get("checked")):
            backed.add("breach_history")
        # BCP / staff training — backed if vendor provided specific dates/frequency
        if intake.get("bcp_last_tested") not in _empty:
            backed.add("business_continuity")
        if intake.get("training_frequency") not in _empty:
            backed.add("staff_training")
        # Third-party — backed if key processors listed
        if intake.get("key_processors") not in _empty:
            backed.add("third_party")
        # Data policy — backed by ACRA/UEN (entity verification)
        if ctx.get("uen") or intake.get("uen"):
            backed.add("data_policy")
        if ctx.get("acra_name") or (acra_live and acra_live.get("found")):
            backed.update({"data_policy", "subcontracting"})
        # GeBIZ supplier — strengthens subcontracting evidence
        if ctx.get("gebiz_supplier"):
            backed.add("subcontracting")
        # SSL grade backs security measures and encryption
        if ssl_result and ssl_result.get("checked"):
            backed.add("security_measures")
            backed.add("encryption_standards")
        return backed

    def _build_docx(
        self,
        company_name: str,
        vendor_url: str,
        qa_answers: Dict[str, str],
        ctx: Dict,
        tx_hash: Optional[str] = None,
        product_type: str = "rfp_complete",
        intake: dict | None = None,
    ) -> bytes:
        """Generate an editable DOCX evidence pack (Complete tier only)."""
        try:
            from docx import Document
            from docx.shared import Pt, RGBColor
            from io import BytesIO

            doc = Document()

            # Title
            title = doc.add_heading("RFP Compliance Evidence Pack", level=0)
            title.runs[0].font.color.rgb = RGBColor(0x10, 0xB9, 0x81)

            intake = intake or {}
            doc.add_paragraph(f"Prepared for: {company_name}")
            doc.add_paragraph(f"Website: {vendor_url}")
            if ctx.get("uen"):
                doc.add_paragraph(f"UEN: {ctx['uen']}")
            if ctx.get("acra_name"):
                doc.add_paragraph(f"ACRA Registered Name: {ctx['acra_name']}")
            if intake.get("dpo_name") or intake.get("dpo_email"):
                dpo_parts = [x for x in [intake.get("dpo_name", ""), intake.get("dpo_email", "")] if x]
                doc.add_paragraph(f"DPO Contact: {', '.join(dpo_parts)}")
            if intake.get("iso_cert_number"):
                expiry = intake.get("iso_cert_expiry", "")
                cert_str = f"ISO 27001 Certificate: {intake['iso_cert_number']}"
                if expiry:
                    cert_str += f" (expires {expiry})"
                doc.add_paragraph(cert_str)
            if ctx.get("privacy_policy_url"):
                doc.add_paragraph(f"Privacy Policy: {ctx['privacy_policy_url']}")
            if ctx.get("gebiz_supplier"):
                count = ctx.get("gebiz_contracts_count") or 0
                doc.add_paragraph(
                    f"GeBIZ Registered Supplier — {count} prior government contract(s)" if count
                    else "GeBIZ Registered Supplier"
                )
            doc.add_paragraph(f"Generated: {datetime.utcnow().strftime('%d %b %Y %H:%M UTC')}")
            doc.add_paragraph(f"Report ID: {self.report_id}")
            if tx_hash:
                doc.add_paragraph(f"Blockchain TX: {tx_hash} (Polygon Amoy Testnet)")
            doc.add_paragraph("")

            doc.add_heading("Compliance Q&A", level=1)
            for key, answer in qa_answers.items():
                doc.add_heading(self._q_label(key), level=2)
                p = doc.add_paragraph(answer)
                p.runs[0].font.size = Pt(11)
                doc.add_paragraph("")  # spacer

            # Attestation section
            doc.add_page_break()
            doc.add_heading("Vendor Attestation", level=1)
            doc.add_paragraph(
                f"I, the authorised representative of {company_name}, hereby attest that the "
                f"information provided in this RFP compliance evidence pack is accurate and "
                f"truthful to the best of my knowledge as of the date stated above."
            )
            doc.add_paragraph("\n\nSignature: _______________________________")
            doc.add_paragraph("Name: _______________________________")
            doc.add_paragraph("Designation: _______________________________")
            doc.add_paragraph("Date: _______________________________")
            doc.add_paragraph("Company Stamp (if applicable): _______________________________")

            buf = BytesIO()
            doc.save(buf)
            buf.seek(0)
            return buf.read()
        except Exception as e:
            logger.error(f"DOCX generation failed: {e}")
            self.errors.append(f"DOCX error: {e}")
            return b""

    # ── Step 4: upload to S3 ──────────────────────────────────────────────────

    async def _write_certificate_log(self, pdf_bytes: bytes, download_url: str, db) -> None:
        """4.11: Write CertificateLog row for audit trail. Skipped for anonymous vendors."""
        import re as _re, hashlib as _hl
        if not db:
            return
        # CertificateLog.vendor_id is a UUID FK — skip if vendor_id is an email
        if not _re.match(r'^[0-9a-f-]{36}$', self.vendor_id or '', _re.IGNORECASE):
            return
        try:
            from app.core.models_v10 import CertificateLog
            import uuid as _uuid
            s3_key = f"rfp-express/{self.report_id}.pdf"
            log = CertificateLog(
                vendor_id=_uuid.UUID(self.vendor_id),
                certificate_type="RFP",
                report_id=_uuid.UUID(self.report_id) if self.report_id else None,
                file_key=s3_key,
                file_hash=_hl.sha256(pdf_bytes).hexdigest() if pdf_bytes else None,
            )
            db.add(log)
            db.commit()
            logger.info(f"CertificateLog written for report {self.report_id}")
        except Exception as e:
            logger.warning(f"CertificateLog write failed (non-blocking): {e}")

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

    async def _upload_docx(self, docx_bytes: bytes) -> Optional[str]:
        try:
            from app.services.storage import S3Service
            import boto3
            s3_svc = S3Service()
            key = f"rfp-complete/{self.report_id}.docx"
            s3_svc.s3_client.put_object(
                Bucket=s3_svc.bucket,
                Key=key,
                Body=docx_bytes,
                ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
            url = s3_svc.s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": s3_svc.bucket, "Key": key},
                ExpiresIn=7 * 24 * 3600,
            )
            logger.info(f"DOCX uploaded: {key}")
            return url
        except Exception as e:
            logger.error(f"DOCX upload failed: {e}")
            self.warnings.append(f"DOCX upload error: {e}")
            return None

    # ── Step 5: email ─────────────────────────────────────────────────────────

    async def _send_email(self, company_name: str, download_url: str, product_type: str = "rfp_express", docx_url: Optional[str] = None):
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
