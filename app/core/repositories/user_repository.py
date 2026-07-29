from typing import Optional
from sqlalchemy import or_
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

    @staticmethod
    def search_users(
        db: Session,
        q: Optional[str] = None,
        role: Optional[str] = None,
        plan: Optional[str] = None,
        is_active: Optional[bool] = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[int, list[User]]:
        """Filtered, paginated user directory for the admin console.

        Returns ``(total_matching, page_rows)`` — the count is of everything the
        filters match, not just this page, so the console can paginate.

        `admin_list_users` has always called this; it was never implemented, so
        `GET /api/v1/admin/users` raised AttributeError and returned 500 for
        every request. Filters are AND-ed; `q` is a case-insensitive substring
        match across email, full name, and company.
        """
        query = db.query(User)

        if q:
            like = f"%{q.strip()}%"
            query = query.filter(
                or_(
                    User.email.ilike(like),
                    User.full_name.ilike(like),
                    User.company.ilike(like),
                )
            )
        if role:
            query = query.filter(User.role == role)
        if plan:
            query = query.filter(User.plan == plan)
        if is_active is not None:
            query = query.filter(User.is_active.is_(is_active))

        # count() before offset/limit so the caller gets the full match count.
        total = query.count()
        rows = (
            query.order_by(User.created_at.desc().nullslast(), User.email.asc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return total, rows
