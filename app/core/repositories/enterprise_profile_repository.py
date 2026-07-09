from typing import List, Optional
from sqlalchemy.orm import Session
from sqlalchemy import desc
from app.core.models import EnterpriseProfile

class EnterpriseProfileRepository:
    @staticmethod
    def get_by_domains(db: Session, domains: List[str]) -> List[EnterpriseProfile]:
        return db.query(EnterpriseProfile).filter(EnterpriseProfile.domain.in_(domains)).all()

    @staticmethod
    def get_by_domain(db: Session, domain: str) -> EnterpriseProfile | None:
        return db.query(EnterpriseProfile).filter(EnterpriseProfile.domain == domain).first()

    @staticmethod
    def count_active_procurement(db: Session) -> int:
        return db.query(EnterpriseProfile).filter(EnterpriseProfile.active_procurement == True).count()

    @staticmethod
    def get_active_procurement_profiles(db: Session, limit: int = 20) -> List[EnterpriseProfile]:
        return (
            db.query(EnterpriseProfile)
            .filter(EnterpriseProfile.active_procurement == True)
            .order_by(desc(EnterpriseProfile.created_at))
            .limit(limit)
            .all()
        )

    @staticmethod
    def get_all_intent_scores(db: Session) -> List[EnterpriseProfile]:
        return db.query(EnterpriseProfile).filter(
            EnterpriseProfile.procurement_intent_score.isnot(None)
        ).all()

    @staticmethod
    def get_top_profiles(db: Session, limit: int = 5) -> List[EnterpriseProfile]:
        return (
            db.query(EnterpriseProfile)
            .filter(EnterpriseProfile.procurement_intent_score.isnot(None))
            .order_by(desc(EnterpriseProfile.procurement_intent_score))
            .limit(limit)
            .all()
        )
