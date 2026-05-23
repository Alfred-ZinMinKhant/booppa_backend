"""
GeBIZ Tender Model
==================
Stores live GeBIZ open tenders fetched by the periodic sync task.
Distinct from TenderShortlist (win-probability catalogue) — this table
holds the real-time tender feed for the Opportunities page and ticker.
"""

import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, Date, Text, Float, Numeric, Index, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID, JSONB
from app.core.db import Base


class GebizTender(Base):
    __tablename__ = "gebiz_tenders"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tender_no = Column(String(100), unique=True, nullable=False, index=True)
    title = Column(Text, nullable=False)
    agency = Column(String(255), nullable=False, index=True)
    closing_date = Column(DateTime, nullable=True, index=True)
    estimated_value = Column(Float, nullable=True)
    status = Column(String(50), nullable=False, default="Open", index=True)
    url = Column(Text, nullable=True)
    raw_data = Column(JSONB, nullable=True)
    last_fetched_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_gebiz_tenders_status_closing", "status", "closing_date"),
    )


class GebizAwardHistory(Base):
    """Persisted row-level award history from data.gov.sg.

    `refresh_gebiz_base_rates` previously aggregated this into per-sector
    base rates and discarded the rows. The Tender Intelligence product
    needs the raw rows for historical award lookup and trend reporting.
    """

    __tablename__ = "gebiz_award_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tender_no = Column(String(100), nullable=True, index=True)
    awarded_date = Column(Date, nullable=True, index=True)
    supplier_name = Column(String(255), nullable=True, index=True)
    award_amt = Column(Numeric(14, 2), nullable=True)
    tender_description = Column(Text, nullable=True)
    procuring_entity = Column(String(255), nullable=True, index=True)
    sector = Column(String(100), nullable=True, index=True)
    raw = Column(JSONB, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "tender_no", "supplier_name", "awarded_date",
            name="uq_gebiz_award_history_tender_supplier_date",
        ),
        Index("ix_gebiz_award_entity_date", "procuring_entity", "awarded_date"),
    )
