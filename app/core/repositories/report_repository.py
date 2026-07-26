from typing import List, Optional
from sqlalchemy.orm import Session
from sqlalchemy import cast, String
from app.core.models import Report

class ReportRepository:
    @staticmethod
    def get_by_id(db: Session, report_id: str) -> Report | None:
        return db.query(Report).filter(Report.id == report_id).first()

    @staticmethod
    def get_by_audit_hash(db: Session, audit_hash: str) -> Report | None:
        return db.query(Report).filter(Report.audit_hash == audit_hash).first()

    @staticmethod
    def get_by_id_and_framework(db: Session, report_id: str, framework: str) -> Report | None:
        return (
            db.query(Report)
            .filter(Report.id == report_id, Report.framework == framework)
            .first()
        )

    # Two session lookups, deliberately distinct names — a bundle checkout can
    # create several stub Reports under one session_id. Keep the names apart:
    # when both were called get_by_stripe_session_id, the list version was
    # silently shadowed and every list call site raised TypeError.
    #
    # `assessment_data` is a plain `json` column, so `CAST(data -> 'k' AS
    # VARCHAR)` yields JSON-quoted text ('"cs_test_…"') and never matches a bare
    # session id. `.as_string()` renders `->>`, which returns unquoted text —
    # don't reintroduce the cast-of-`->` form.
    @staticmethod
    def _session_id_matches(session_id: str):
        return Report.assessment_data["stripe_session_id"].as_string() == session_id

    @staticmethod
    def list_by_stripe_session_id(db: Session, session_id: str) -> list[Report]:
        """All Reports for a session, newest first."""
        return (
            db.query(Report)
            .filter(ReportRepository._session_id_matches(session_id))
            .order_by(Report.created_at.desc())
            .all()
        )

    @staticmethod
    def get_by_stripe_session_id(db: Session, session_id: str) -> Report | None:
        """Most recent Report for a session."""
        return (
            db.query(Report)
            .filter(ReportRepository._session_id_matches(session_id))
            .order_by(Report.created_at.desc())
            .first()
        )

    @staticmethod
    def get_latest_for_owner_by_framework(
        db: Session, owner_id: str, framework: str, status: Optional[str] = None
    ) -> Report | None:
        query = db.query(Report).filter(
            Report.owner_id == owner_id, Report.framework == framework
        )
        if status:
            query = query.filter(Report.status == status)
        return query.order_by(Report.created_at.desc()).first()

    @staticmethod
    def get_latest_for_owner_by_frameworks(
        db: Session, owner_id: str, frameworks: List[str]
    ) -> Report | None:
        return (
            db.query(Report)
            .filter(Report.owner_id == owner_id, Report.framework.in_(frameworks))
            .order_by(Report.created_at.desc())
            .first()
        )
