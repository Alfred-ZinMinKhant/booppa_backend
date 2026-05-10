"""
Enterprise Package models — V12
Organisations, SSO, Webhooks, MAS TRM, White-label
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey,
    Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.core.db import Base


class Organisation(Base):
    __tablename__ = "organisations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    slug = Column(String(100), unique=True, nullable=False)
    tier = Column(String(50), default="standard")          # standard | pro | custom
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Subsidiary(Base):
    __tablename__ = "subsidiaries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id"), nullable=False)
    name = Column(String(255), nullable=False)
    uen = Column(String(50))
    country = Column(String(100), default="Singapore")
    created_at = Column(DateTime, default=datetime.utcnow)


class OrganisationMember(Base):
    __tablename__ = "organisation_members"
    __table_args__ = (UniqueConstraint("organisation_id", "user_id"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    role = Column(String(50), default="member")            # owner | admin | member
    created_at = Column(DateTime, default=datetime.utcnow)


class WebhookEndpoint(Base):
    __tablename__ = "webhook_endpoints"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id"), nullable=False)
    url = Column(Text, nullable=False)
    secret = Column(String(128), nullable=False)           # used for HMAC-SHA256 signing
    events = Column(JSONB, default=list)                   # ["report.ready", "lead.hot", ...]
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    endpoint_id = Column(UUID(as_uuid=True), ForeignKey("webhook_endpoints.id"), nullable=False)
    event_type = Column(String(100), nullable=False)
    payload = Column(JSONB)
    status_code = Column(Integer)
    response_body = Column(Text)
    success = Column(Boolean, default=False)
    attempt = Column(Integer, default=1)
    delivered_at = Column(DateTime, default=datetime.utcnow)


# MAS TRM 13-domain controls
MAS_TRM_DOMAINS = [
    "Technology Risk Governance",
    "IT Project and Change Management",
    "Technology Operations",
    "IT Outsourcing and Vendor Management",
    "Cyber Security",
    "Data and Information Management",
    "Customer Awareness and Education",
    "Incident Management",
    "IT Audit",
    "Business Continuity and Disaster Recovery",
    "Technology Testing",
    "Cloud Computing",
    "Authentication and Access Management",
]


class TrmControl(Base):
    __tablename__ = "trm_controls"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id"), nullable=False)
    domain = Column(String(100), nullable=False)           # one of MAS_TRM_DOMAINS
    control_ref = Column(String(50))                       # e.g. "TRM-5.2"
    description = Column(Text)
    status = Column(String(30), default="not_started")     # not_started | in_progress | compliant | gap
    gap_analysis = Column(Text)                            # AI-generated gap narrative
    risk_rating = Column(String(20))                       # low | medium | high | critical
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TrmEvidence(Base):
    __tablename__ = "trm_evidence"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    control_id = Column(UUID(as_uuid=True), ForeignKey("trm_controls.id"), nullable=False)
    file_name = Column(String(255))
    s3_key = Column(Text)
    hash_value = Column(String(64))
    tx_hash = Column(String(66))                           # blockchain anchor
    uploaded_at = Column(DateTime, default=datetime.utcnow)


class RetentionPolicy(Base):
    __tablename__ = "retention_policies"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id"), nullable=False)
    data_category = Column(String(100), nullable=False)    # e.g. "personal_data", "audit_logs"
    retention_days = Column(Integer, nullable=False)
    auto_purge = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class SsoConfig(Base):
    __tablename__ = "sso_configs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id"), unique=True, nullable=False)
    protocol = Column(String(20), nullable=False)          # saml | oidc
    # SAML fields
    idp_metadata_url = Column(Text)
    idp_entity_id = Column(Text)
    sp_acs_url = Column(Text)
    # OIDC fields
    client_id = Column(String(255))
    client_secret = Column(String(255))
    discovery_url = Column(Text)
    # Common
    is_active = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class WhiteLabelConfig(Base):
    __tablename__ = "white_label_configs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id"), unique=True, nullable=False)
    logo_s3_key = Column(Text)
    primary_color = Column(String(7), default="#10b981")   # hex
    secondary_color = Column(String(7), default="#0f172a")
    footer_text = Column(Text)
    report_header_text = Column(Text)
    custom_domain = Column(String(255))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SlaLog(Base):
    __tablename__ = "sla_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id"), nullable=False)
    event_type = Column(String(100), nullable=False)
    target_minutes = Column(Integer)
    actual_minutes = Column(Integer)
    met = Column(Boolean)
    event_metadata = Column("metadata", JSONB, default=dict)
    recorded_at = Column(DateTime, default=datetime.utcnow)


# ── Organisation invites (team collaboration) ────────────────────────────────
class OrganisationInvite(Base):
    __tablename__ = "organisation_invites"
    __table_args__ = (UniqueConstraint("organisation_id", "email", name="uq_org_invite_email"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False)
    email = Column(String(255), nullable=False, index=True)
    role = Column(String(50), default="member")            # admin | member
    token = Column(String(64), unique=True, nullable=False, index=True)
    invited_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    status = Column(String(20), default="pending")         # pending | accepted | revoked | expired
    expires_at = Column(DateTime, nullable=False)
    accepted_at = Column(DateTime, nullable=True)
    accepted_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


# ── Shared vendor watchlist (team collaboration) ─────────────────────────────
class VendorWatchlistItem(Base):
    __tablename__ = "vendor_watchlist_items"
    __table_args__ = (UniqueConstraint("organisation_id", "vendor_ref", name="uq_watchlist_org_vendor"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False, index=True)
    # vendor_ref accepts either a marketplace vendor slug or a free-form identifier so
    # we don't need to FK directly to a single vendor table (multiple vendor models exist).
    vendor_ref = Column(String(255), nullable=False)
    vendor_name = Column(String(255), nullable=True)
    notes = Column(Text, nullable=True)
    added_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ── Per-vendor comments inside an org's watchlist ────────────────────────────
class VendorWatchlistComment(Base):
    __tablename__ = "vendor_watchlist_comments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    watchlist_item_id = Column(UUID(as_uuid=True), ForeignKey("vendor_watchlist_items.id", ondelete="CASCADE"), nullable=False, index=True)
    author_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
