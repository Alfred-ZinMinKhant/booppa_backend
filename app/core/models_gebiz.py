"""
GeBIZ Tender Model
==================
Stores live GeBIZ open tenders fetched by the periodic sync task.
Distinct from TenderShortlist (win-probability catalogue) — this table
holds the real-time tender feed for the Opportunities page and ticker.
"""

import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, Text, Float, Index
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
