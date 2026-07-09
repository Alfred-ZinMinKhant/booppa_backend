from typing import List, Optional
from sqlalchemy.orm import Session
from app.core.models import VerifyRecord

class VerifyRecordRepository:
    @staticmethod
    def get_by_vendor_id(db: Session, vendor_id: str) -> VerifyRecord | None:
        return db.query(VerifyRecord).filter(VerifyRecord.vendor_id == vendor_id).first()

    @staticmethod
    def get_verified_by_vendor_id(db: Session, vendor_id: str) -> VerifyRecord | None:
        return db.query(VerifyRecord).filter(
            VerifyRecord.vendor_id == vendor_id,
            VerifyRecord.status == "verified"
        ).first()

    @staticmethod
    def get_by_id(db: Session, verify_id: str) -> VerifyRecord | None:
        return db.query(VerifyRecord).filter(VerifyRecord.id == verify_id).first()

    @staticmethod
    def active_exists_by_vendor_id(db: Session, vendor_id: str) -> bool:
        from app.core.models import LifecycleStatus
        return (
            db.query(VerifyRecord)
            .filter(
                VerifyRecord.vendor_id == vendor_id,
                VerifyRecord.lifecycle_status == LifecycleStatus.ACTIVE,
            )
            .first()
            is not None
        )
