from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
import qrcode
from io import BytesIO
import logging
from datetime import datetime
import base64

logger = logging.getLogger(__name__)
import os
from app.core.config import settings


class PDFService:
    """PDF generation service with QR code integration"""

    def __init__(self):
        self.styles = getSampleStyleSheet()
        self.setup_custom_styles()

    def setup_custom_styles(self):
        """Setup custom PDF styles"""
        # Avoid re-adding styles if they already exist in the stylesheet
        try:
            if "Title" not in self.styles.byName:
                self.styles.add(
                    ParagraphStyle(
                        name="Title",
                        parent=self.styles.get("Heading1"),
                        fontSize=18,
                        spaceAfter=30,
                        textColor=colors.HexColor("#2E86AB"),
                    )
                )
        except Exception:
            logger.debug("PDF style 'Title' already exists or could not be added")

        try:
            if "Heading2" not in self.styles.byName:
                self.styles.add(
                    ParagraphStyle(
                        name="Heading2",
                        parent=self.styles.get("Heading2"),
                        fontSize=14,
                        spaceAfter=12,
                        textColor=colors.HexColor("#A23B72"),
                    )
                )
        except Exception:
            logger.debug("PDF style 'Heading2' already exists or could not be added")

    def generate_pdf(self, report_data: dict) -> bytes:
        """Generate PDF report with QR code"""
        try:
            buffer = BytesIO()
            doc = SimpleDocTemplate(
                buffer,
                pagesize=A4,
                leftMargin=inch,
                rightMargin=inch,
                topMargin=inch,
                bottomMargin=inch,
            )
            story = []

            # Title
            story.append(Paragraph("BOOPPA AUDIT REPORT", self.styles["Title"]))
            story.append(Spacer(1, 0.1 * inch))

            # Subtitle: company and generated date
            company = report_data.get("company_name", "")
            gen = report_data.get("created_at", "")
            subtitle = f"{company} — {gen}" if company or gen else ""
            if subtitle:
                story.append(Paragraph(subtitle, self.styles["BodyText"]))
                story.append(Spacer(1, 0.15 * inch))

            # Proof metadata header (paid tiers)
            proof_block = self._create_proof_metadata(report_data)
            if proof_block:
                story.extend(proof_block)
                story.append(Spacer(1, 0.15 * inch))

            # Optional: include site screenshot if provided (bytes or base64 string)
            ss = report_data.get("site_screenshot")
            if ss:
                try:
                    img_data = None
                    if isinstance(ss, str):
                        # assume base64-encoded string
                        img_data = base64.b64decode(ss)
                    elif isinstance(ss, bytes):
                        img_data = ss
                    elif hasattr(ss, "read"):
                        img_data = ss.read()

                    if img_data:
                        img_buffer = BytesIO(img_data)
                        # render screenshot at reasonable size
                        story.append(
                            Paragraph("Site Screenshot", self.styles["Heading2"])
                        )
                        story.append(Spacer(1, 0.05 * inch))
                        story.append(
                            Image(img_buffer, width=5.5 * inch, height=3.5 * inch)
                        )
                        story.append(Spacer(1, 0.15 * inch))
                except Exception as e:
                    logger.warning(f"Failed to render site screenshot: {e}")

            # Report details
            story.append(Paragraph("Report Details", self.styles["Heading2"]))
            story.extend(self._create_detail_paragraphs(report_data))
            story.append(Spacer(1, 0.1 * inch))

            # Key Issues Found (if present)
            if report_data.get("key_issues"):
                story.append(Paragraph("Key Issues Found", self.styles["Heading2"]))
                for issue in report_data.get("key_issues"):
                    story.append(Paragraph(f"• {issue}", self.styles["Bullet"]))
                story.append(Spacer(1, 0.08 * inch))

                # Mandatory PDPA action/warning section inserted immediately after Key Issues
                story.extend(self._create_mandatory_pdpa_warning(report_data))
                story.append(Spacer(1, 0.15 * inch))

            # Blockchain verification
            # Blockchain verification (shows pending verification if not anchored yet)
            story.append(Paragraph("Blockchain Verification", self.styles["Heading2"]))
            story.extend(self._create_blockchain_section(report_data))
            story.append(Spacer(1, 0.2 * inch))

            # If a structured report is provided, render full sections (executive summary, findings, recommendations)
            structured = None
            if report_data.get("structured_report") and isinstance(
                report_data.get("structured_report"), dict
            ):
                structured = report_data.get("structured_report")
            else:
                # Allow keys at top-level (backwards compatibility)
                if any(
                    k in report_data
                    for k in (
                        "executive_summary",
                        "detailed_findings",
                        "recommendations",
                        "legal_references",
                    )
                ):
                    structured = report_data

            if structured:
                # Executive summary
                exec_sum = structured.get("executive_summary")
                if exec_sum:
                    story.append(
                        Paragraph("Executive Summary", self.styles["Heading2"])
                    )
                    for part in [
                        p.strip() for p in exec_sum.split("\n\n") if p.strip()
                    ]:
                        story.append(
                            Paragraph(part.replace("\n", " "), self.styles["BodyText"])
                        )
                        story.append(Spacer(1, 0.08 * inch))

                # Detailed findings
                findings = structured.get("detailed_findings", [])
                if findings:
                    story.append(
                        Paragraph("Detailed Findings", self.styles["Heading2"])
                    )
                    for i, f in enumerate(findings, 1):
                        f_type = f.get("type", "Finding").replace("_", " ").title()
                        severity = f.get("severity", "MEDIUM")
                        desc = (
                            f.get("description")
                            or f.get("details")
                            or "No description provided"
                        )
                        evidence = f.get("evidence") or ""
                        penalty = (
                            f.get("penalty", {}).get("amount")
                            if isinstance(f.get("penalty"), dict)
                            else None
                        )

                        story.append(
                            Paragraph(
                                f"{i}. {severity} — {f_type}", self.styles["BodyText"]
                            )
                        )
                        story.append(
                            Paragraph(desc.replace("\n", " "), self.styles["BodyText"])
                        )
                        if evidence:
                            story.append(
                                Paragraph(
                                    f"Evidence: {evidence}", self.styles["BodyText"]
                                )
                            )
                        if penalty:
                            story.append(
                                Paragraph(
                                    f"Penalty: {penalty}", self.styles["BodyText"]
                                )
                            )
                        story.append(Spacer(1, 0.08 * inch))

                # Recommendations
                recs = structured.get("recommendations", [])
                if recs:
                    story.append(Paragraph("Recommendations", self.styles["Heading2"]))
                    for i, r in enumerate(recs, 1):
                        vtype = (
                            r.get("violation_type", "").replace("_", " ").title()
                            if isinstance(r.get("violation_type"), str)
                            else ""
                        )
                        story.append(
                            Paragraph(
                                f"{i}. {vtype} — {r.get('severity', '')}",
                                self.styles["BodyText"],
                            )
                        )
                        actions = r.get("actions", [])
                        for a in actions:
                            story.append(Paragraph(f"• {a}", self.styles["BodyText"]))
                        timeline = r.get("timeline")
                        if timeline:
                            story.append(
                                Paragraph(
                                    f"Timeline: {timeline}", self.styles["BodyText"]
                                )
                            )
                        story.append(Spacer(1, 0.08 * inch))

                # Legal references
                refs = structured.get("legal_references", [])
                if refs:
                    story.append(Paragraph("Legal References", self.styles["Heading2"]))
                    for ref in refs:
                        title = ref.get("title") if isinstance(ref, dict) else str(ref)
                        url = ref.get("url") if isinstance(ref, dict) else None
                        if url:
                            story.append(
                                Paragraph(
                                    f'• {title}: <a href="{url}">{url}</a>',
                                    self.styles["BodyText"],
                                )
                            )
                        else:
                            story.append(
                                Paragraph(f"• {title}", self.styles["BodyText"])
                            )
                    story.append(Spacer(1, 0.08 * inch))
            else:
                if report_data.get("ai_narrative"):
                    story.append(Paragraph("AI Analysis", self.styles["Heading2"]))
                    # Split narrative into paragraphs for better layout
                    narrative = report_data.get("ai_narrative", "")
                    for part in [
                        p.strip() for p in narrative.split("\n\n") if p.strip()
                    ]:
                        story.append(
                            Paragraph(part.replace("\n", " "), self.styles["BodyText"])
                        )
                        story.append(Spacer(1, 0.08 * inch))

            # Build PDF
            # PDPA/legal disclaimer is always included
            story.extend(self._create_pdpa_disclaimer())
            doc.build(story)
            buffer.seek(0)

            logger.info("PDF generated successfully")
            return buffer.getvalue()

        except Exception as e:
            logger.error(f"PDF generation failed: {e}")
            raise

    def _create_detail_paragraphs(self, report_data: dict) -> list:
        """Create report detail paragraphs"""
        details = [
            f"<b>Report ID:</b> {report_data.get('report_id', 'N/A')}",
            f"<b>Framework:</b> {report_data.get('framework', 'N/A')}",
            f"<b>Company:</b> {report_data.get('company_name', 'N/A')}",
            f"<b>Generated:</b> {report_data.get('created_at', datetime.utcnow().isoformat())}",
            f"<b>Status:</b> {report_data.get('status', 'completed')}",
        ]

        paragraphs = []
        for detail in details:
            paragraphs.append(Paragraph(detail, self.styles["BodyText"]))
            paragraphs.append(Spacer(1, 0.1 * inch))

        return paragraphs

    def _create_mandatory_pdpa_warning(self, report_data: dict) -> list:
        """Create the mandatory PDPA warning block and purchase links"""
        sections = []

        # Warning header and text
        sections.append(
            Paragraph(
                "<b>Important: Why Action is Required Immediately</b>",
                self.styles["Heading2"],
            )
        )
        warning_text = (
            "We believe it is critical to bring this to your attention for two strategic reasons:\n\n"
            "Regulatory Enforcement: Under the updated PDPA, the PDPC has the authority to impose financial penalties of up to $1 million SGD or 10% of your annual turnover in Singapore, whichever is higher.\n\n"
            "Competitive & Reputation Risk: In Singapore’s transparent market, compliance gaps are increasingly monitored not only by regulators but also by market competitors. A formal report to the authorities by a third party regarding non-compliant data practices could trigger an immediate investigation and cause irreversible reputational damage."
        )
        sections.append(
            Paragraph(warning_text.replace("\n", "<br/>"), self.styles["BodyText"])
        )
        sections.append(Spacer(1, 0.1 * inch))

        # Purchase links - use prefill_email param
        prefill = report_data.get("contact_email") or "evidence@booppa.io"
        # Determine base URL: prefer explicit report_data base, then BACKEND_BASE_URL env,
        # otherwise construct from app settings (fallback to localhost when APP_HOST is 0.0.0.0)
        host = (
            settings.APP_HOST
            if getattr(settings, "APP_HOST", "0.0.0.0") != "0.0.0.0"
            else "localhost"
        )
        default_base = f"http://{host}:{getattr(settings, 'APP_PORT', '8000')}"
        base = (
            report_data.get("base_url")
            or os.environ.get("BACKEND_BASE_URL")
            or default_base
        )

        products = [
            (
                "PDPA Quick Scan ($69.00 SGD)",
                f"{base}/api/stripe/checkout?product=pdpa_quick_scan&prefill_email={prefill}",
            ),
            (
                "PDPA Essential ($299.00 SGD/mo)",
                f"{base}/api/stripe/checkout?product=pdpa_basic&prefill_email={prefill}",
            ),
            (
                "Standard Suite ($1,299.00 SGD/mo)",
                f"{base}/api/stripe/checkout?product=compliance_standard&prefill_email={prefill}",
            ),
            (
                "Pro Suite ($1,999.00 SGD/mo)",
                f"{base}/api/stripe/checkout?product=compliance_pro&prefill_email={prefill}",
            ),
        ]

        sections.append(
            Paragraph("<b>Fix it now — Purchase Options</b>", self.styles["Heading2"])
        )
        for label, url in products:
            # clickable link
            sections.append(
                Paragraph(f'- <a href="{url}">{label}</a>', self.styles["BodyText"])
            )
            sections.append(Spacer(1, 0.05 * inch))

        return sections

    def _create_blockchain_section(self, report_data: dict) -> list:
        """Create blockchain verification section with QR code"""
        sections = []
        tx_hash = report_data.get("tx_hash")
        audit_hash = report_data.get("audit_hash")
        payment_confirmed = report_data.get("payment_confirmed", False)
        verify_url = report_data.get("verify_url")

        # Blockchain details
        sections.append(
            Paragraph(f"<b>Transaction Hash:</b> {tx_hash}", self.styles["BodyText"])
        )
        sections.append(Spacer(1, 0.1 * inch))

        if audit_hash:
            sections.append(
                Paragraph(
                    f"<b>Evidence Hash:</b> {audit_hash}", self.styles["BodyText"]
                )
            )
            sections.append(Spacer(1, 0.1 * inch))

        # Prefer verify URL for paid reports
        if payment_confirmed and verify_url and audit_hash:
            sections.append(
                Paragraph(
                    f'<b>Verification URL:</b> <a href="{verify_url}">{verify_url}</a>',
                    self.styles["BodyText"],
                )
            )
            sections.append(Spacer(1, 0.1 * inch))
            qr_target = verify_url
        # If payment not confirmed or tx_hash absent, point to pending verification page
        elif not payment_confirmed or not tx_hash:
            pending_url = (
                report_data.get("pending_verification_url")
                or f"https://www.booppa.io/verify/pending?report_id={report_data.get('report_id') }"
            )
            sections.append(
                Paragraph(
                    f'<b>Verification URL:</b> <a href="{pending_url}">Pending Verification</a>',
                    self.styles["BodyText"],
                )
            )
            sections.append(Spacer(1, 0.1 * inch))
            qr_target = pending_url
        else:
            # CRITICAL FIX: Zero spaces in Polygonscan URL for valid QR codes
            polygonscan_url = f"https://polygonscan.com/tx/{tx_hash}"
            sections.append(
                Paragraph(
                    f'<b>Verification URL:</b> <a href="{polygonscan_url}">{polygonscan_url}</a>',
                    self.styles["BodyText"],
                )
            )
            sections.append(Spacer(1, 0.1 * inch))
            qr_target = polygonscan_url
        sections.append(Spacer(1, 0.2 * inch))

        # Generate QR code
        try:
            qr = qrcode.QRCode(version=1, box_size=4, border=2)
            qr.add_data(qr_target)
            qr.make(fit=True)

            qr_img = qr.make_image(fill_color="black", back_color="white")
            qr_buffer = BytesIO()
            qr_img.save(qr_buffer, format="PNG")
            qr_buffer.seek(0)

            qr_image = Image(qr_buffer, width=1.5 * inch, height=1.5 * inch)
            sections.append(
                Paragraph("<b>Scan to Verify:</b>", self.styles["BodyText"])
            )
            sections.append(Spacer(1, 0.1 * inch))
            sections.append(qr_image)

        except Exception as e:
            logger.warning(f"QR code generation failed: {e}")
            sections.append(
                Paragraph("<i>QR code unavailable</i>", self.styles["Italic"])
            )

        return sections

    def _create_proof_metadata(self, report_data: dict) -> list:
        """Create proof metadata header for paid reports."""
        proof_header = report_data.get("proof_header")
        schema_version = report_data.get("schema_version")
        verify_url = report_data.get("verify_url")

        if not (proof_header or schema_version or verify_url):
            return []

        sections = [
            Paragraph("Proof Metadata", self.styles["Heading2"]),
        ]
        if proof_header:
            sections.append(
                Paragraph(f"<b>Format:</b> {proof_header}", self.styles["BodyText"])
            )
        if schema_version:
            sections.append(
                Paragraph(
                    f"<b>Schema Version:</b> {schema_version}",
                    self.styles["BodyText"],
                )
            )
        if verify_url:
            sections.append(
                Paragraph(
                    f'<b>Verify URL:</b> <a href="{verify_url}">{verify_url}</a>',
                    self.styles["BodyText"],
                )
            )
        return sections

    def _create_pdpa_disclaimer(self) -> list:
        """Add PDPA/legal disclaimer and prohibit certification/regulatory claims."""
        disclaimer = (
            "This report is provided for informational purposes only and does not "
            "constitute legal advice, certification, or regulatory approval. "
            "Booppa does not certify vendors, issue regulatory determinations, or "
            "publish public vendor scoring. Organizations should consult qualified "
            "professionals for compliance decisions and regulatory engagement."
        )
        return [
            Spacer(1, 0.2 * inch),
            Paragraph("Disclaimer", self.styles["Heading2"]),
            Paragraph(disclaimer, self.styles["BodyText"]),
        ]
