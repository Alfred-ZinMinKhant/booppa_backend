from typing import List
from sqlalchemy.orm import Session
from app.core.models import Referral

class ReferralRepository:
    @staticmethod
    def get_by_referred_email(db: Session, email: str) -> Referral | None:
        return db.query(Referral).filter(Referral.referred_email == email).first()

    @staticmethod
    def get_by_code(db: Session, code: str) -> Referral | None:
        return db.query(Referral).filter(Referral.referral_code == code).first()

    @staticmethod
    def get_pending_by_referrer_id(db: Session, referrer_id: str) -> Referral | None:
        return db.query(Referral).filter(
            Referral.referrer_id == referrer_id,
            Referral.status == "PENDING"
        ).first()

    @staticmethod
    def get_by_referrer_id(db: Session, referrer_id: str) -> List[Referral]:
        return db.query(Referral).filter(Referral.referrer_id == referrer_id).all()

    @staticmethod
    def get_unclaimed_by_referred_id_for_update(db: Session, referred_id: str) -> Referral | None:
        return (
            db.query(Referral)
            .filter(
                Referral.referred_id == referred_id,
                Referral.status == "SIGNED_UP",
                Referral.reward_claimed == False,
            )
            .with_for_update()
            .first()
        )
