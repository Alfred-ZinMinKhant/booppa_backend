import uuid
from sqlalchemy import (
    Column,
    String,
    DateTime,
    Text,
    JSON,
    Boolean,
    Date,
    ForeignKey,
    Integer,
)
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

    # V6 Additions for Vendor features
    role = Column(String(50), default="VENDOR")
    company = Column(String(255), nullable=True)
    uen = Column(String(50), unique=True, nullable=True)
    plan = Column(String(20), default="free", nullable=False, server_default="free")
    temp_password = Column(Boolean, default=False)
    verified_at = Column(DateTime, nullable=True)
    subscription_tier = Column(String(50), nullable=True)
    subscription_started_at = Column(DateTime, nullable=True)
    stripe_customer_id = Column(String(255), nullable=True)
    stripe_subscription_id = Column(String(255), nullable=True)
    website = Column(String(500), nullable=True)
    industry = Column(String(100), nullable=True)
    company_description = Column(Text, nullable=True)

    # Notarization credits granted by bundle purchases (compliance_evidence_pack,
    # vendor_trust_pack, rfp_accelerator, enterprise_bid_kit). Decremented when the
    # user uploads a document at /notarize. Distinct from monthly enterprise credits
    # (NotarizationCredit table) which are subscription-based.
    notarization_credits = Column(Integer, default=0, nullable=False, server_default="0")


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


class AuditChainEvent(Base):
    __tablename__ = "audit_chain_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    report_id = Column(
        UUID(as_uuid=True), ForeignKey("reports.id"), nullable=False, index=True
    )
    action = Column(String(100), nullable=False, index=True)
    actor = Column(String(255), nullable=False)
    hash_prev = Column(String(64), nullable=False)
    hash = Column(String(64), nullable=False, index=True)
    metadata_json = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


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


class HardenedConsent(Base):
    __tablename__ = "hardened_consents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_email = Column(String(255), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(String(500), nullable=True)
    legal_version = Column(String(100), nullable=False, default="v17_Hardened")
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)


class DemoBooking(Base):
    __tablename__ = "demo_bookings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slot_id = Column(String(32), nullable=False, index=True)
    slot_date = Column(Date, nullable=False, index=True)
    start_time = Column(String(5), nullable=False)
    end_time = Column(String(5), nullable=False)

    customer_name = Column(String(255), nullable=False)
    customer_email = Column(String(255), nullable=False, index=True)
    customer_phone = Column(String(50), nullable=True)
    notes = Column(Text, nullable=True)

    status = Column(String(32), default="confirmed", index=True)
    booking_token = Column(String(32), unique=True, index=True, nullable=False)
    source = Column(String(50), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticket_id = Column(String(50), unique=True, nullable=False, index=True)
    tracking_token = Column(String(64), unique=True, nullable=False, index=True)

    name = Column(String(255), nullable=False)
    email = Column(String(255), nullable=False, index=True)
    category = Column(String(50), nullable=False)
    subject = Column(String(500), nullable=False)
    message = Column(Text, nullable=False)

    status = Column(String(32), default="open", index=True)
    priority = Column(String(32), default="medium", index=True)
    assigned_to = Column(String(255), nullable=True)

    ip_address = Column(String(45), nullable=True)
    user_agent = Column(String(500), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SupportTicketReply(Base):
    __tablename__ = "support_ticket_replies"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticket_id = Column(String(50), nullable=False, index=True)
    author = Column(String(255), nullable=False)
    author_type = Column(String(20), nullable=False, default="staff")
    message = Column(Text, nullable=False)
    is_internal = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class ResourceItem(Base):
    __tablename__ = "resource_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    category = Column(String(100), nullable=False, index=True)
    title = Column(String(500), nullable=False)
    description = Column(Text, nullable=True)
    href = Column(String(500), nullable=False)
    sort_order = Column(Integer, default=0)
    is_active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ProcessedWebhookEvent(Base):
    """Idempotency guard: records every Stripe event ID we have processed."""

    __tablename__ = "processed_webhook_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(String(255), unique=True, nullable=False, index=True)
    event_type = Column(String(100), nullable=True)
    processed_at = Column(DateTime, default=datetime.utcnow, index=True)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    stripe_subscription_id = Column(
        String(255), unique=True, nullable=False, index=True
    )
    stripe_customer_id = Column(String(255), nullable=True, index=True)
    product_type = Column(String(100), nullable=True)
    status = Column(String(50), nullable=True, index=True)
    current_period_end = Column(DateTime, nullable=True)
    # `metadata` is a reserved attribute on Declarative base; expose as `metadata_json`
    metadata_json = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# Import V6 extensions so Alembic picks them up correctly
from .models_v6 import *

# Import V8 extensions (VendorStatusSnapshot, ScoreSnapshot, NotarizationMetadata,
# RfpRequirement, RfpRequirementFlag)
from .models_v8 import *

# Import V11 extensions (ComplianceRequirement, ManagedVendor)
from .models_v11 import *

# Import V12 Enterprise extensions
from .models_enterprise import *
