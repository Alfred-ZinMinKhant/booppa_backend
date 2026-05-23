"""
Vendor Pro — supporting models.

TenderCheckLookup: append-only log of /tender-check calls. Powers the
competitor-awareness signal surfaced to Vendor Pro subscribers (counts
of verified vendors who probed a tender, no identities exposed).
"""

import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, Boolean, Integer, Index
from sqlalchemy.dialects.postgresql import UUID
from app.core.db import Base


class TenderCheckLookup(Base):
    __tablename__ = "tender_check_lookups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tender_no = Column(String(100), index=True, nullable=True)
    vendor_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    # Denormalised sector to make sector-match queries cheap.
    sector = Column(String(100), nullable=True, index=True)
    # Was the looking-up vendor a verified vendor at the time of lookup?
    is_verified = Column(Boolean, default=False, index=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True, nullable=False)

    __table_args__ = (
        Index("ix_tender_check_lookups_tender_created", "tender_no", "created_at"),
        Index("ix_tender_check_lookups_sector_created", "sector", "created_at"),
    )
