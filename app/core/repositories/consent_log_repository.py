from typing import List
from sqlalchemy.orm import Session
from app.core.models import ConsentLog

class ConsentLogRepository:
    @staticmethod
    def get_recent_logs(db: Session, limit: int = 50) -> List[ConsentLog]:
        return (
            db.query(ConsentLog)
            .order_by(ConsentLog.timestamp.desc())
            .limit(limit)
            .all()
        )
