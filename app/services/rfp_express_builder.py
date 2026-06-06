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
from datetime import datetime, timedelta, timezone
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
        self.generation_start     = datetime.now(timezone.utc)

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

        # 2b. Post-AI validation pass — catch AI hallucinations against intake.
        # LLMs over-confidently fill in DPO names, ISO certifications, hosting
        # regions, etc. even when the intake declared otherwise. We surface
        # these on the result page so the buyer fixes them before submitting.
        # Merged into the consistency-check discrepancies below so they land in
        # the PDF warnings + frontend `discrepancies` array exactly once.
        ai_discrepancies = self._validate_answers_against_intake(qa_answers, intake)

        # Consistency check — intake vs external evidence
        discrepancies = check_consistency(intake, website_text, pdpc_result, domain_rep)
        discrepancies = (discrepancies or []) + ai_discrepancies
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

        elapsed = (datetime.now(timezone.utc) - self.generation_start).total_seconds()
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
            "pdf_s3_key":     getattr(self, "pdf_s3_key", None),
            "docx_url":       docx_url,
            "qa_answers":     qa_display,
            "qa_answers_count": len(qa_display),
            "tx_hash":        tx_hash,
            "polygonscan_url": f"{explorer_base}/tx/{tx_hash}" if tx_hash else None,
            "network":        settings.POLYGON_NETWORK_NAME,
            "testnet_notice": settings.POLYGON_TESTNET_NOTICE,
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
            "expires_at":     (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
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
                    external_facts.append(
                        "- External domain reputation: no public security incidents or malicious flags detected. "
                        "For breach_history answer, state: 'No data breaches have occurred to the knowledge of management. "
                        "No external security incidents have been publicly reported against the company\\'s domains.' "
                        "Do NOT mention VirusTotal or any scanning tool names in the answer."
                    )

            if external_facts:
                external_section = "Verified external data:\n" + "\n".join(external_facts)
                facts_section = "\n\n".join(filter(None, [facts_section, external_section]))

            context_block = "\n\n".join(filter(None, [facts_section, website_section]))

            # Anti-fabrication prompt. Buyers were getting answers full of
            # specifics the LLM invented — "ISO 27001:2022 certified", "AES-256",
            # "99.99% uptime", "patched within 24 hours", "data in Singapore on
            # AWS and Azure". If submitted unread to a GeBIZ tender, those are
            # false statements in a government procurement context. Three rules:
            #   1. Anchor every specific to a known fact (intake / website /
            #      external data). No fact → no specific.
            #   2. When a fact is missing, write a "[Verify: ...]" placeholder.
            #      The buyer fills it in. The reviewer SEES the placeholder.
            #   3. Answer the EXACT question asked (matters for windows like
            #      "24 months" — the AI was answering "36 months" on intake-empty
            #      runs because nothing constrained it).
            prompt = (
                f"You are drafting RFP compliance answers for {ctx['company_name']}"
                f"{sector_hint} (website: {ctx['vendor_url']}).{rfp_hint}\n\n"
                + (f"{context_block}\n\n" if context_block else "")
                + "Write concise, professional answers for a Singapore government procurement RFP. "
                "Each answer must be 1-3 sentences.\n\n"
                "ABSOLUTE RULES — do not break these even if the answer reads less polished:\n\n"
                "1. NEVER invent specifics. Do not name any of the following unless the exact value "
                "appears in the facts above:\n"
                "   • Certifications or standards (e.g. ISO 27001, ISO 27701, SOC 2 — including versions/years)\n"
                "   • Encryption algorithms or protocols (e.g. AES-256, TLS 1.2/1.3, RSA-2048)\n"
                "   • Specific timeframes (e.g. '24 hours', 'within 7 days', 'weekly', 'quarterly')\n"
                "   • Uptime SLAs or percentages (e.g. '99.99%')\n"
                "   • Cloud providers, regions, or vendor names (e.g. AWS, Azure, GCP, Singapore region)\n"
                "   • Specific personnel names, contact emails, or DPO names\n"
                "   • Retention periods, audit log durations, or breach-notification windows\n"
                "2. When a specific would normally go but the fact is missing, write a bracketed "
                "placeholder the buyer must fill in: '[Verify: encryption standard]', "
                "'[Verify: ISO 27001 cert number and expiry]', '[Verify: BCP last test date]', "
                "'[Verify: SLA target]'. The placeholder is REQUIRED, not optional.\n"
                "3. Answer the EXACT question asked. If the question asks about '24 months', do NOT "
                "expand the window to 36 months. If the question asks 'Yes/No', begin with Yes or No.\n"
                "4. Do NOT cite legal obligations with specific numbers unless they are correct under "
                "Singapore PDPA. Breach notification to PDPC is 'within 3 calendar days' (PDPA §26D) — "
                "do NOT write '1 hour' or 'immediately'. The recipient is PDPC, not 'the government'.\n"
                "5. Do NOT speculate about technical architecture details (blockchain nodes, smart "
                "contract audits, threat monitoring) unless they appear verbatim in the facts above.\n\n"
                f"Return ONLY a JSON object with these exact keys:\n{keys_list}."
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

    # Audit fix A: per-answer prefix so evaluators know answers are templated.
    # Audit fix B (2026): template answers no longer assert specific facts the
    # buyer may not actually meet (e.g. "AES-256", "Singapore residency",
    # "RTO of 4 hours") — these turned a "starter draft" into a perjury risk
    # if a rushed buyer submitted unread to a GeBIZ tender. Specifics are now
    # `___ [FILL IN] ___` placeholders the buyer MUST replace before submitting.
    _TEMPLATE_PREFIX = "[Standard template — review and replace [FILL IN] fields before submission] "
    _FI = "___ [FILL IN] ___"

    def _template_qa(self, ctx: Dict, questions: list) -> Dict[str, str]:
        name = ctx["company_name"]
        url  = ctx["vendor_url"]
        p    = self._TEMPLATE_PREFIX
        fi   = self._FI
        all_answers = {
            "data_policy":          f"{p}{name} maintains a PDPA-compliant Personal Data Protection Policy, accessible at {url}. Personal data is collected with consent and retained only for its stated purpose.",
            "dpo_appointed":        f"{p}DPO appointment status: {fi} (Yes/No/In progress). If appointed, name: {fi}, email: {fi}, PDPC registration: {fi}.",
            "security_measures":    f"{p}{name} implements technical and organisational security controls including {fi} (e.g. access controls, encryption in transit, staff training). See encryption_standards and access_controls for specifics.",
            "breach_history":       f"{p}Breach history (last 24 months): {fi} (None / Yes — describe). Any PDPC-notifiable incidents must be disclosed here.",
            "third_party":          f"{p}{name} requires Data Processing Agreements (DPAs) before sharing personal data with sub-processors. Key processors in scope: {fi}.",
            "iso_certifications":   f"{p}ISO 27001 status: {fi} (Certified / Pursuing / None). If certified, certificate number: {fi}, expiry: {fi}. SOC 2 status: {fi}.",
            "business_continuity":  f"{p}{name} maintains a BCP/DR plan. Last tested: {fi}. RTO target: {fi}. RPO target: {fi}.",
            "staff_training":       f"{p}Security awareness training frequency: {fi} (e.g. annual, quarterly). New-hire training window: {fi}.",
            "access_controls":      f"{p}{name} enforces role-based access control with least-privilege principles. Privileged access review cadence: {fi}. MFA on privileged accounts: {fi} (Yes/No).",
            "vulnerability_mgmt":   f"{p}Patch SLA for critical vulnerabilities: {fi}. Vulnerability scan cadence: {fi}.",
            "encryption_standards": f"{p}Encryption at rest: {fi} (e.g. AES-256). Encryption in transit: {fi} (e.g. TLS 1.2+). Key management process: {fi}.",
            "audit_logging":        f"{p}Audit log retention period: {fi} months. Log monitoring/anomaly detection: {fi} (describe).",
            "incident_response":    f"{p}{name} maintains an Incident Response Plan. Internal notification window: {fi}. PDPC notification is made within 3 business days if the incident is PDPC-notifiable.",
            "data_residency":       f"{p}Primary data hosting region: {fi}. Cloud provider: {fi}. Cross-border transfers (if any) are governed by contractual clauses consistent with PDPA's Third Schedule.",
            "subcontracting":       f"{p}Subcontracting / offshoring of personal data processing: {fi} (None / Yes — list arrangements). Any engagements require prior written approval and binding DPAs.",
        }
        return {k: all_answers[k] for k in questions if k in all_answers}

    def _validate_answers_against_intake(self, qa_answers: Dict[str, str], intake: dict) -> list[str]:
        """Cross-check AI-generated answers against buyer-supplied intake facts.

        Two failure modes covered:
          (a) Contradiction — intake says X, answer says NOT X (e.g. intake
              dpo_appointed='no' but answer asserts a DPO exists).
          (b) Fabrication — intake has no value for X, but answer asserts a
              specific value anyway (e.g. intake silent on ISO 27001, answer
              claims "ISO 27001:2022 certified"). Submitting fabricated
              specifics to a GeBIZ tender is a legal risk under the Government
              Procurement Act.

        The original prompt is the first defence (see _generate_qa). This is the
        belt-and-suspenders pass that catches what the LLM lets through.

        Returns a list of human-readable discrepancy strings (may be empty).
        """
        if not isinstance(qa_answers, dict) or not isinstance(intake, dict):
            return []

        out: list[str] = []
        import re

        def lower(key: str) -> str:
            v = qa_answers.get(key)
            return v.lower() if isinstance(v, str) else ""

        # ── DPO ─────────────────────────────────────────────────────────────
        dpo_state = (intake.get("dpo_appointed") or "").lower()
        dpo_ans = lower("dpo_appointed")
        if dpo_state == "no" and dpo_ans:
            # Buyer declared no DPO, but answer claims one exists.
            if re.search(r"\b(has appointed|appointed a|dpo is|our dpo|the dpo)\b", dpo_ans) \
               and not re.search(r"\b(no dpo|has not appointed|not yet appointed|in progress|to be appointed)\b", dpo_ans):
                out.append(
                    "Intake declares no DPO appointed, but the dpo_appointed answer reads as if one exists. "
                    "Edit the answer to match the intake before submission."
                )

        # ── ISO 27001 ───────────────────────────────────────────────────────
        iso_state = (intake.get("iso_status") or "").lower()
        iso_ans = lower("iso_certifications")
        if iso_state in {"none", "pursuing", "in_progress", "unknown", ""} and iso_ans:
            if re.search(r"\bis (iso[\s-]?27001\s)?certified\b|\bholds (an? )?iso[\s-]?27001 certification\b|\biso[\s-]?27001 certified\b", iso_ans):
                out.append(
                    f"Intake declares ISO 27001 status as '{iso_state or 'unknown'}', but the answer asserts "
                    f"the company is certified. Correct the answer to match the intake."
                )
        if iso_state == "certified" and not intake.get("iso_cert_number"):
            out.append(
                "ISO 27001 declared certified in intake but no certificate number supplied — evaluators cannot verify. "
                "Add iso_cert_number to the intake or remove the certified claim."
            )

        # ── Breach history ──────────────────────────────────────────────────
        breach_state = (intake.get("breach_history") or "").lower()
        breach_ans = lower("breach_history")
        if breach_state in {"one", "multiple"} and breach_ans:
            if re.search(r"\bno (data )?breach(es)?\b|\bno security incidents?\b|\bnever experienced\b", breach_ans):
                out.append(
                    f"Intake declares {breach_state} breach(es) in the last 24 months, but the answer says none. "
                    "PDPC-notifiable breaches MUST be disclosed in tender responses."
                )

        # ── Data hosting region ────────────────────────────────────────────
        hosting = (intake.get("data_hosting") or "").lower()
        residency_ans = lower("data_residency")
        if hosting == "global" and residency_ans:
            if re.search(r"\b(all|only|exclusively).{0,30}\b(singapore|sg)\b", residency_ans):
                out.append(
                    "Intake declares global data hosting, but the data_residency answer claims Singapore-only. "
                    "Edit to disclose actual cross-border hosting."
                )
        if hosting in {"apac", "global"} and residency_ans:
            if re.search(r"\bdoes not (offshore|cross[- ]border transfer)\b", residency_ans):
                out.append(
                    f"Intake declares data_hosting='{hosting}' (non-SG), but the answer says no cross-border transfer. "
                    "Reconcile before submission."
                )

        # ── Fabricated specifics (fires when intake has no backing fact) ────
        # These patterns catch the LLM inventing certifications, encryption
        # standards, uptime SLAs, timeframes, cloud providers etc. when the
        # intake didn't supply them. We *don't* flag if the buyer actually
        # supplied a corresponding fact — they earned the right to that claim.

        def claims(key: str, pattern: str) -> bool:
            v = qa_answers.get(key)
            return bool(isinstance(v, str) and re.search(pattern, v, re.IGNORECASE))

        # ISO / SOC 2 / equivalent — flag specific certs/years when intake silent
        if iso_state in {"", "unknown", "none", "pursuing", "in_progress"}:
            for key in ("iso_certifications", "security_measures", "data_policy"):
                if claims(key, r"\biso[\s-]?2700[17](?::\s?\d{4})?\b|\bsoc[\s-]?2\b|\biso[\s-]?9001\b"):
                    out.append(
                        f"The '{key}' answer names a specific certification (ISO 27001 / SOC 2 / similar) but "
                        "the intake did not confirm any. Remove the certification claim or supply the "
                        "intake.iso_status + iso_cert_number to back it up."
                    )
                    break

        # Encryption algorithms — flag AES-N / TLS X.Y / RSA-N when intake silent
        if not (intake.get("encryption_standards_known") or intake.get("encryption_at_rest") or intake.get("encryption_in_transit")):
            for key in ("encryption_standards", "security_measures", "access_controls"):
                if claims(key, r"\baes[-\s]?(?:128|192|256)\b|\btls\s?1\.\d\b|\brsa[-\s]?\d{3,4}\b"):
                    out.append(
                        f"The '{key}' answer names a specific encryption standard (AES-N / TLS X.Y / RSA-N) "
                        "but the intake did not declare one. Confirm the actual standard used or replace "
                        "the specific with a [Verify: encryption standard] placeholder."
                    )
                    break

        # Uptime SLAs — flag 99.9...% when intake silent
        if not intake.get("uptime_sla"):
            for key in ("business_continuity", "security_measures", "incident_response"):
                if claims(key, r"\b99\.9{1,3}\s?%|\bfive[- ]nines?\b|\bfour[- ]nines?\b"):
                    out.append(
                        f"The '{key}' answer commits to a specific uptime SLA (99.9...% / five-nines) "
                        "but the intake did not declare one. Remove the percentage or supply the "
                        "actual SLA via intake."
                    )
                    break

        # Specific timeframes/cadences — flag "weekly / daily / monthly / N hours / N days"
        # in answers that should be backed by intake but weren't supplied.
        cadence_pat = r"\b(?:weekly|daily|monthly|quarterly|semi[- ]annually|annually|bi[- ]monthly)\b|\bwithin\s+\d+\s+(?:minute|hour|day|week|month)s?\b|\bevery\s+\d+\s+(?:minute|hour|day|week|month)s?\b"
        if not intake.get("training_frequency") and claims("staff_training", cadence_pat):
            out.append(
                "The 'staff_training' answer commits to a specific cadence but the intake did not "
                "declare one. Edit to your actual training schedule or replace with a [Verify: cadence] placeholder."
            )
        if not intake.get("bcp_last_tested") and claims("business_continuity", r"\btested\s+(?:weekly|daily|monthly|quarterly|semi[- ]annually|annually)\b"):
            out.append(
                "The 'business_continuity' answer commits to a BCP test cadence but the intake did "
                "not declare when the BCP was last tested. Confirm the actual cadence or use a placeholder."
            )
        # Vulnerability management — patch-SLA and scan-cadence specifics
        if claims("vulnerability_mgmt", r"\bpatched?\s+within\s+\d+\s+(?:hour|day)s?\b|\b(?:weekly|daily|monthly|quarterly)\s+(?:vulnerability scans?|penetration tests?)\b|\bwithin\s+\d+\s+(?:hour|day)s?\s+of\b"):
            out.append(
                "The 'vulnerability_mgmt' answer names a specific patch SLA or scan cadence. "
                "Confirm against your actual policy or replace with [Verify: patch SLA] / "
                "[Verify: scan cadence] placeholders."
            )
        # Audit log retention — flag specific N-year / N-month retention
        if claims("audit_logging", r"\bretain(?:ed)?\s+(?:for\s+)?(?:a\s+minimum\s+of\s+)?\d+\s+(?:months?|years?)\b|\b\d+[- ](?:month|year)\s+retention\b"):
            out.append(
                "The 'audit_logging' answer commits to a specific log retention period. "
                "Confirm against policy or replace with [Verify: retention period]."
            )
        # Breach-window mismatch in the answer itself (e.g. AI answered "36 months"
        # when the question asks about 24 months).
        if claims("breach_history", r"\bpast\s+(?:3[6-9]|[4-9]\d|\d{3,})\s+months?\b|\b(?:3|4|5)\s+years?\b"):
            out.append(
                "The 'breach_history' answer references a window longer than the 24 months the "
                "question asks about. Re-answer for the 24-month window only."
            )

        # Cloud providers — flag AWS / Azure / GCP / OCI when intake silent
        if not intake.get("primary_cloud"):
            for key in ("data_residency", "security_measures", "encryption_standards"):
                if claims(key, r"\baws\b|\bamazon web services\b|\bazure\b|\bmicrosoft azure\b|\bgcp\b|\bgoogle cloud\b|\boracle cloud\b|\boci\b"):
                    out.append(
                        f"The '{key}' answer names a specific cloud provider (AWS/Azure/GCP/etc.) but "
                        "the intake did not declare one. Confirm the actual provider or use a placeholder."
                    )
                    break

        # PDPC breach-notification window — must be 3 calendar days (PDPA §26D),
        # NOT "1 hour" / "immediately" / "24 hours" / "to the government".
        ir_ans = lower("incident_response")
        if ir_ans:
            if re.search(r"\bwithin\s+1\s+hour\b|\bwithin\s+24\s+hours?\b|\bimmediately\s+(?:notify|inform)\b", ir_ans):
                out.append(
                    "The 'incident_response' answer commits to a breach-notification window that does "
                    "not match PDPA §26D (3 calendar days for notifiable breaches). Correct the window."
                )
            if re.search(r"\bnotify(?:ing)?\s+the\s+government\b|\binform(?:ing)?\s+the\s+government\b", ir_ans):
                out.append(
                    "The 'incident_response' answer says breaches are notified to 'the government'. "
                    "Under PDPA §26D, notification is to the PDPC (Personal Data Protection Commission), "
                    "not 'the government'. Correct the recipient."
                )

        return out

    # ── Step 2.5: blockchain anchor ───────────────────────────────────────────

    async def _anchor_to_blockchain(self) -> Optional[str]:
        try:
            import hashlib
            from app.services.blockchain import BlockchainService
            from app.core.config import settings
            # report_id is a UUID string; derive a 64-char SHA-256 hex so it is
            # valid input for the EvidenceAnchorV3 bytes32 contract parameter.
            evidence_hash = hashlib.sha256(self.report_id.encode()).hexdigest()
            blockchain = BlockchainService()
            tx = await blockchain.anchor_evidence(
                evidence_hash,
                metadata=f"rfp_express:vendor:{self.vendor_id}",
            )
            logger.info(f"RFP Express anchored on {settings.POLYGON_NETWORK_NAME}: {tx}")
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

            # Pre-flight review checklist — one line per answer with an initials
            # slot. Forces the buyer to manually acknowledge each answer before
            # submission instead of paste-and-pray. Lives at the top of the Q&A
            # section so it's the first thing the reader hits.
            checklist_lines = [
                f"  [  ]  {self._q_label(k)} — reviewed, accurate, no [FILL IN] left.    Initials: ______"
                for k in qa_answers.keys()
            ]
            checklist = (
                "PRE-FLIGHT REVIEW CHECKLIST\n"
                "Tick each box and initial after you have read the corresponding answer, "
                "confirmed it reflects your company's actual practice, and replaced any "
                "[FILL IN] placeholders. Do NOT submit this document if any box is unticked.\n\n"
                + "\n".join(checklist_lines)
                + "\n\n  [  ]  All [FILL IN] placeholders have been replaced with real, accurate values.    Initials: ______"
                + "\n  [  ]  I confirm the company has the capability to substantiate every claim above.    Initials: ______"
                + "\n  [  ]  This document is being submitted alongside the proposal, pricing, and team sections."
                + "    Initials: ______"
                + "\n\nReviewer name: ______________________________    Date: __________    Signature: ______________________"
                + "\n─────────────────────────────────────────────────────────────────────\n"
            )

            qa_section = "\n\n".join(
                f"Q: {self._q_label(k)}\nA: {v}"
                for k, v in qa_answers.items()
            )

            explorer_base = settings.POLYGON_EXPLORER_URL.rstrip("/")
            blockchain_info = (
                f"Blockchain TX: {tx_hash}\n"
                f"Network: {settings.POLYGON_NETWORK_NAME}\n"
                f"Note: {settings.POLYGON_TESTNET_NOTICE}\n"
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
                f"UEN (Singapore Business Registration No.): {ctx.get('uen') or '_______________________________'}",
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
            # Scope banner — kit covers PDPA + security only; buyer must add
            # the rest of the bid (proposal, pricing, team) themselves. Without
            # this, "Ready-to-submit bid kit" framing mis-sets expectations.
            # Lives in ai_narrative (not summary) because PDFService renders
            # ai_narrative as visible paragraphs in this product; summary feeds
            # the structured cover and isn't shown verbatim.
            scope_banner = (
                "⚠ SCOPE NOTICE — READ BEFORE SUBMITTING\n"
                "This document covers PDPA and information-security compliance answers only. "
                "It is NOT a complete bid response. Before submitting to GeBIZ or any other "
                "procurement portal, you must add your own:\n"
                "  • Technical proposal\n"
                "  • Pricing and commercial terms\n"
                "  • Delivery timeline and milestones\n"
                "  • Team and key personnel\n"
                "  • Tender-specific requirements (references, case studies, certifications attached)\n"
                "Review every answer below and replace any [FILL IN] placeholders before submission. "
                "Booppa does not warrant the suitability of these answers for any particular tender; "
                "the buyer remains responsible for the accuracy of submitted statements.\n"
                "─────────────────────────────────────────────────────────────────────\n\n"
            )

            report_data = {
                "company_name": company_name,
                "report_id":    self.report_id,
                "created_at":   datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC"),
                "framework":    framework_label,
                "product_type": product_type,
                "summary":      (
                    f"This certificate confirms that {company_name} has completed the "
                    f"BOOPPA {'RFP Kit Complete' if is_complete else 'RFP Kit Express'} process, "
                    f"generating blockchain-anchored evidence for procurement submission.\n\n"
                    f"Scope of Assessment: This compliance pack is based on information provided by the "
                    f"company's authorised representative and automated website assessment conducted by "
                    f"Booppa on the date indicated.\n\n"
                    f"{vendor_details}"
                ),
                "key_issues":   [],
                # Use ai_narrative so PDFService renders as plain paragraphs
                # (not structured mode). Order matters: scope banner → checklist
                # → Q&A — reader hits the scope notice first, then the checklist
                # they must initial, only then the actual answers.
                "ai_narrative": scope_banner + checklist + "\n" + qa_section,
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
            from app.core.config import settings

            doc = Document()

            # Title
            title = doc.add_heading("RFP Compliance Evidence Pack", level=0)
            title.runs[0].font.color.rgb = RGBColor(0x10, 0xB9, 0x81)

            intake = intake or {}
            doc.add_paragraph(f"Prepared for: {company_name}")
            doc.add_paragraph(f"Website: {vendor_url}")
            uen_val = ctx.get("uen") or "_______________________________"
            doc.add_paragraph(f"UEN (Singapore Business Registration No.): {uen_val}")
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
            doc.add_paragraph(f"Generated: {datetime.now(timezone.utc).strftime('%d %b %Y %H:%M UTC')}")
            doc.add_paragraph(f"Report ID: {self.report_id}")
            if tx_hash:
                doc.add_paragraph(f"Blockchain TX: {tx_hash} ({settings.POLYGON_NETWORK_NAME})")
            doc.add_paragraph("")
            scope_para = doc.add_paragraph(
                "Scope of Assessment: This compliance pack is based on information provided by the "
                "company's authorised representative and automated website assessment conducted by "
                "Booppa on the date indicated."
            )
            scope_para.runs[0].italic = True
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
            # Use the real S3 key set by _upload_pdf so downstream readers can
            # presign the URL. Falls back to the (legacy, wrong) express path
            # only if _upload_pdf wasn't reached for some reason.
            s3_key = getattr(self, "pdf_s3_key", None) or f"rfp-express/{self.report_id}.pdf"
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
            self.pdf_s3_key = f"reports/{folder}/{self.report_id}.pdf"
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
