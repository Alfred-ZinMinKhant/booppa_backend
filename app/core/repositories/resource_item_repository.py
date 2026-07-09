from typing import List, Optional
from sqlalchemy.orm import Session
from sqlalchemy import desc
from app.core.models import ResourceItem

class ResourceItemRepository:
    @staticmethod
    def list_active_ordered(db: Session) -> List[ResourceItem]:
        return (
            db.query(ResourceItem)
            .filter(ResourceItem.is_active == True)
            .order_by(ResourceItem.category, ResourceItem.sort_order, ResourceItem.created_at)
            .all()
        )

    @staticmethod
    def get_by_id(db: Session, item_id: str) -> ResourceItem | None:
        return db.query(ResourceItem).filter(ResourceItem.id == item_id).first()
