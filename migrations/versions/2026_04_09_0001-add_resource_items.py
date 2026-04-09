"""Add resource_items table with seed data

Revision ID: 2026_04_09_0001
Revises: 2026_04_02_0001
Create Date: 2026-04-09
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
import uuid
from datetime import datetime

revision = "2026_04_09_0001"
down_revision = "2026_04_02_0001"
branch_labels = None
depends_on = None

SEED_ITEMS = [
    # RFP Tips
    ("RFP Tips", "How to Win Government Tenders in Singapore", "A practical guide to GeBIZ submission requirements, common disqualification reasons, and how to prepare procurement-ready evidence.", "/blog", 1),
    ("RFP Tips", "PDPA Compliance Checklist for SMEs", "The 8 PDPA obligations every Singapore SME must address before submitting to enterprise procurement portals.", "/pdpa", 2),
    ("RFP Tips", "RFP Compliance Section: What Procurement Teams Actually Check", "Inside view of what procurement evaluators look for in vendor compliance sections — and how to pass every time.", "/blog", 3),
    # Compliance Education
    ("Compliance Education", "PDPA in Plain English", "The Personal Data Protection Act explained without the legal jargon. What it means for your business and what you need to document.", "/pdpa", 1),
    ("Compliance Education", "Blockchain Notarization vs Legal Notarization", "What blockchain timestamping can and cannot do — and when each type of notarization is appropriate for your documents.", "/notarization", 2),
    ("Compliance Education", "Singapore MAS TRM Guidelines: Key Requirements", "Technology Risk Management requirements for financial institutions — what evidence you need and how BOOPPA helps you build it.", "/compliance", 3),
    # Vendor Guides
    ("Vendor Guides", "How to Claim Your BOOPPA Vendor Profile", "Step-by-step guide to claiming, completing, and getting verified on the BOOPPA Vendor Network.", "/auth/register", 1),
    ("Vendor Guides", "Building a Procurement-Ready Evidence Package", "What documents to include in your RFP evidence package and how to structure them for maximum credibility.", "/rfp-acceleration", 2),
    ("Vendor Guides", "Understanding Your Tender Win Probability Score", "How the BOOPPA CAL engine scores your tender eligibility and what actions improve your probability.", "/tender-check", 3),
]


def upgrade() -> None:
    op.create_table(
        "resource_items",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("category", sa.String(100), nullable=False, index=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("href", sa.String(500), nullable=False),
        sa.Column("sort_order", sa.Integer, default=0),
        sa.Column("is_active", sa.Boolean, default=True),
        sa.Column("created_at", sa.DateTime, default=datetime.utcnow),
        sa.Column("updated_at", sa.DateTime, default=datetime.utcnow),
    )

    op.bulk_insert(
        sa.table(
            "resource_items",
            sa.column("id", UUID(as_uuid=True)),
            sa.column("category", sa.String),
            sa.column("title", sa.String),
            sa.column("description", sa.Text),
            sa.column("href", sa.String),
            sa.column("sort_order", sa.Integer),
            sa.column("is_active", sa.Boolean),
            sa.column("created_at", sa.DateTime),
            sa.column("updated_at", sa.DateTime),
        ),
        [
            {
                "id": uuid.uuid4(),
                "category": cat,
                "title": title,
                "description": desc,
                "href": href,
                "sort_order": order,
                "is_active": True,
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }
            for cat, title, desc, href, order in SEED_ITEMS
        ],
    )


def downgrade() -> None:
    op.drop_table("resource_items")
