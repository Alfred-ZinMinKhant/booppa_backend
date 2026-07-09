from typing import Optional
from sqlalchemy.orm import Session
from app.core.models import User

class UserRepository:
    @staticmethod
    def get_by_email(
        db: Session, email: str, lock_for_update: bool = False
    ) -> User | None:
        query = db.query(User).filter(User.email == email)
        if lock_for_update:
            query = query.with_for_update()
        return query.first()

    @staticmethod
    def get_by_id(db: Session, user_id: str) -> User | None:
        return db.query(User).filter(User.id == user_id).first()
