from typing import List, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import date
from app.core.models import DemoBooking

class BookingRepository:
    @staticmethod
    def get_booked_slot_counts(db: Session, start_date: date, end_date: date) -> List[Tuple[str, int]]:
        rows = (
            db.query(DemoBooking.slot_id, func.count(DemoBooking.id))
            .filter(
                DemoBooking.status == "confirmed",
                DemoBooking.slot_date >= start_date,
                DemoBooking.slot_date <= end_date,
            )
            .group_by(DemoBooking.slot_id)
            .all()
        )
        return rows

    @staticmethod
    def get_slot_booking_count(db: Session, slot_id: str) -> int:
        return (
            db.query(func.count(DemoBooking.id))
            .filter(
                DemoBooking.slot_id == slot_id,
                DemoBooking.status == "confirmed",
            )
            .scalar() or 0
        )

    @staticmethod
    def get_by_token(db: Session, token: str) -> DemoBooking | None:
        return (
            db.query(DemoBooking)
            .filter(DemoBooking.booking_token == token)
            .first()
        )
