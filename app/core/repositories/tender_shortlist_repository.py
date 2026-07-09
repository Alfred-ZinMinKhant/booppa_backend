from typing import List, Optional
from sqlalchemy.orm import Session
from app.core.models import TenderShortlist

class TenderShortlistRepository:
    @staticmethod
    def get_by_tender_no(db: Session, tender_no: str) -> TenderShortlist | None:
        return db.query(TenderShortlist).filter(TenderShortlist.tender_no == tender_no).first()

    @staticmethod
    def get_by_id(db: Session, uid: str) -> TenderShortlist | None:
        return db.query(TenderShortlist).filter(TenderShortlist.id == uid).first()

    @staticmethod
    def list_entries(
        db: Session, 
        sector: Optional[str] = None, 
        agency: Optional[str] = None, 
        offset: int = 0, 
        limit: int = 50
    ) -> tuple[int, List[TenderShortlist]]:
        q = db.query(TenderShortlist)
        if sector:
            q = q.filter(TenderShortlist.sector == sector)
        if agency:
            q = q.filter(TenderShortlist.agency == agency)
        total = q.count()
        rows = q.order_by(TenderShortlist.created_at.desc()).offset(offset).limit(limit).all()
        return total, rows
