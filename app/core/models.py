import uuid
from sqlalchemy import Column, String, DateTime, Text, JSON, Boolean
from sqlalchemy.dialects.postgresql import UUID
from datetime import datetime
from app.core.db import Base
import uuid


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    full_name = Column(String(255))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Report(Base):
    __tablename__ = "reports"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    framework = Column(String(100), nullable=False)
    company_name = Column(String(255), nullable=False)
    company_website = Column(String(500), nullable=True)
    assessment_data = Column(JSON, nullable=False)

    # Processing status
    status = Column(String(50), default="pending", index=True)

    # Blockchain evidence
    audit_hash = Column(String(64), nullable=True)
    tx_hash = Column(String(66), nullable=True, index=True)

    # Storage
    s3_url = Column(Text, nullable=True)
    file_key = Column(String(500), nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    # AI Narrative
    ai_narrative = Column(Text, nullable=True)
    ai_model_used = Column(String(100), nullable=True)


class TaskLock(Base):
    __tablename__ = "task_locks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(String(255), unique=True, nullable=False, index=True)
    locked_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)


class ConsentLog(Base):
    __tablename__ = "consent_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    ip_anonymized = Column(String(64), nullable=True)
    consent_status = Column(String(50), nullable=False, index=True)
    policy_version = Column(String(100), nullable=True)
    # `metadata` is a reserved attribute name on the Declarative base, so
    # store JSON metadata in the database column named "metadata" but expose
    # it on the model as `metadata_json` to avoid conflicts.
    metadata_json = Column("metadata", JSON, nullable=True)
