"""
Booppa V12 (continued) — API keys
=================================
Webhooks, SSO, and Organisation membership models already live in
`models_enterprise.py` (org-keyed). This file only adds ApiKey, which is
user-scoped — your API token authenticates as you regardless of organisation.

Multi-subsidiary uses `parent_user_id` on the users table — see the matching
Alembic migration.
"""

import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID
from app.core.db import Base


class ApiKey(Base):
    __tablename__ = "api_keys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(120), nullable=False)
    prefix = Column(String(16), nullable=False, index=True)
    hashed_key = Column(String(64), nullable=False, unique=True, index=True)
    last_used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


Index("ix_api_keys_user_active", ApiKey.user_id, ApiKey.revoked_at)
