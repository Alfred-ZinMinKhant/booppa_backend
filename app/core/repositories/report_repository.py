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
    def get_by_stripe_session_id(db: Session, session_id: str) -> list[Report]:
        from sqlalchemy import String, cast
        return (
            db.query(Report)
            .filter(cast(Report.assessment_data["stripe_session_id"], String) == session_id)
            .all()
        )

    @staticmethod
    def get_by_id_and_framework(db: Session, report_id: str, framework: str) -> Report | None:
        return (
            db.query(Report)
            .filter(Report.id == report_id, Report.framework == framework)
            .first()
        )

    @staticmethod
    def get_by_stripe_session_id(db: Session, session_id: str) -> Report | None:
        return (
            db.query(Report)
            .filter(cast(Report.assessment_data["stripe_session_id"], String) == session_id)
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
