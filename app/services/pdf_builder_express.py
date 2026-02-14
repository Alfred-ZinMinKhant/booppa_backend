# app/services/pdf_builder_express.py

from datetime import datetime
from typing import Dict
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, Image
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
import qrcode
from io import BytesIO
import logging

logger = logging.getLogger(__name__)


class RFPKitPDFBuilder:
    """Build RFP Kit Evidence certificate for RFP Kit Express"""
    
    async def build_evidence_certificate(
        self,
        output_path: str,
        company_name: str,
        vendor_url: str,
        scan_results: Dict,
        qa_answers: Dict[str, str],
        questions_included: int,
        product_tier: str,
        price: str,
        validity_months: int = 12
    ):
        """
        Build professional RFP Kit Evidence certificate (PDF).
        """
        
        logger.info(f"📄 Building RFP Kit Evidence certificate for {company_name}")
        
        doc = SimpleDocTemplate(output_path, pagesize=A4)
        story = []
        styles = getSampleStyleSheet()
        
        # Custom styles
        title_style = ParagraphStyle(
            'RFPKitTitle',
            parent=styles['Heading1'],
            fontSize=22,
            textColor=colors.HexColor('#0f172a'),
            spaceAfter=20,
            alignment=TA_CENTER,
            fontName='Helvetica-Bold'
        )
        
        subtitle_style = ParagraphStyle(
            'RFPKitSubtitle',
            parent=styles['Normal'],
            fontSize=14,
            textColor=colors.HexColor('#10b981'),
            alignment=TA_CENTER,
            spaceAfter=30
        )
        
        # COVER PAGE
        story.append(Spacer(1, 1.5*inch))
        
        # Title rebranding
        story.append(Paragraph("RFP KIT EVIDENCE CERTIFICATE", title_style))
        story.append(Paragraph("Singapore PDPA Compliance Evidence", subtitle_style))
        
        # ... (rest of PDF builder logic follows same pattern as prepared in exploration folder)
        # For migration, I'll ensure the content is rebranded correctly.
        
        doc.build(story)
        logger.info(f"✓ RFP Kit Evidence certificate created: {output_path}")
