from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import List, Optional
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.models import DemoBooking

router = APIRouter()


def _parse_int_list(value: str, fallback: List[int]) -> List[int]:
    try:
        return [int(v.strip()) for v in value.split(",") if v.strip() != ""]
    except Exception:
        return fallback


def _get_slot_config():
    working_days = _parse_int_list(settings.BOOKING_WORKING_DAYS, [0, 1, 2, 3, 4])
    morning = _parse_int_list(settings.BOOKING_MORNING_SLOTS, [9, 10, 11])
    afternoon = _parse_int_list(settings.BOOKING_AFTERNOON_SLOTS, [14, 15, 16])
    hours = morning + afternoon
    return working_days, hours


def _generate_slots(days_ahead: int):
    try:
        tz = ZoneInfo(settings.BOOKING_TIMEZONE)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    today = datetime.now(tz).date()
    working_days, hours = _get_slot_config()

    slots = []
    for day in range(days_ahead):
        slot_date = today + timedelta(days=day)
        if slot_date.weekday() not in working_days:
            continue

        for hour in hours:
            slot_id = f"{slot_date.isoformat()}-{hour:02d}"
            start_time = f"{hour:02d}:00"
            end_time = f"{hour + 1:02d}:00"
            slots.append(
                {
                    "slot_id": slot_id,
                    "slot_date": slot_date.isoformat(),
                    "start_time": start_time,
                    "end_time": end_time,
                }
            )
    return slots


def _validate_slot_id(slot_id: str, days_ahead: int):
    try:
        date_part, hour_part = slot_id.rsplit("-", 1)
        slot_date = datetime.strptime(date_part, "%Y-%m-%d").date()
        hour = int(hour_part)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid slot_id")

    try:
        tz = ZoneInfo(settings.BOOKING_TIMEZONE)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    now_local = datetime.now(tz)
    today = now_local.date()
    if slot_date < today or slot_date > today + timedelta(days=days_ahead - 1):
        raise HTTPException(status_code=400, detail="Slot date out of range")

    working_days, hours = _get_slot_config()
    if slot_date.weekday() not in working_days or hour not in hours:
        raise HTTPException(status_code=400, detail="Slot not available")

    # Prevent booking past times (local timezone)
    slot_start = datetime(
        slot_date.year, slot_date.month, slot_date.day, hour, 0, tzinfo=tz
    )
    if slot_start <= now_local:
        raise HTTPException(status_code=400, detail="Slot is in the past")

    return slot_date, hour


class SlotItem(BaseModel):
    id: str
    slot_date: str
    start_time: str
    end_time: str
    remaining: int
    available: bool


class BookingCreate(BaseModel):
    slot_id: str = Field(..., min_length=8)
    customer_name: str = Field(..., min_length=2, max_length=255)
    customer_email: EmailStr
    customer_phone: Optional[str] = Field(default=None, max_length=50)
    notes: Optional[str] = Field(default=None, max_length=2000)


class BookingResponse(BaseModel):
    id: str
    booking_token: str
    slot_date: str
    start_time: str
    end_time: str
    status: str


@router.get("/slots", response_model=List[SlotItem])
def list_slots(days: int = 30):
    days = max(1, min(days, settings.BOOKING_DAYS_AHEAD))
    slots = _generate_slots(days)

    tz = ZoneInfo(settings.BOOKING_TIMEZONE)
    today = datetime.now(tz).date()
    end_date = today + timedelta(days=days - 1)

    db = SessionLocal()
    try:
        rows = (
            db.query(DemoBooking.slot_id, func.count(DemoBooking.id))
            .filter(
                DemoBooking.status == "confirmed",
                DemoBooking.slot_date >= today,
                DemoBooking.slot_date <= end_date,
            )
            .group_by(DemoBooking.slot_id)
            .all()
        )
        counts = {r[0]: r[1] for r in rows}

        results = []
        for s in slots:
            booked = counts.get(s["slot_id"], 0)
            remaining = max(settings.BOOKING_MAX_PER_SLOT - booked, 0)
            results.append(
                {
                    "id": s["slot_id"],
                    "slot_date": s["slot_date"],
                    "start_time": s["start_time"],
                    "end_time": s["end_time"],
                    "remaining": remaining,
                    "available": remaining > 0,
                }
            )
        return results
    finally:
        db.close()


@router.post("", response_model=BookingResponse)
def create_booking(payload: BookingCreate):
    days_ahead = settings.BOOKING_DAYS_AHEAD
    slot_date, hour = _validate_slot_id(payload.slot_id, days_ahead)

    start_time = f"{hour:02d}:00"
    end_time = f"{hour + 1:02d}:00"

    db = SessionLocal()
    try:
        existing = (
            db.query(func.count(DemoBooking.id))
            .filter(
                DemoBooking.slot_id == payload.slot_id,
                DemoBooking.status == "confirmed",
            )
            .scalar()
        )

        if existing and existing >= settings.BOOKING_MAX_PER_SLOT:
            raise HTTPException(status_code=400, detail="Time slot is full")

        booking = DemoBooking(
            slot_id=payload.slot_id,
            slot_date=slot_date,
            start_time=start_time,
            end_time=end_time,
            customer_name=payload.customer_name,
            customer_email=str(payload.customer_email),
            customer_phone=payload.customer_phone,
            notes=payload.notes,
            status="confirmed",
            booking_token=uuid.uuid4().hex[:16],
            source="web",
        )
        db.add(booking)
        db.commit()
        db.refresh(booking)

        return BookingResponse(
            id=str(booking.id),
            booking_token=booking.booking_token,
            slot_date=booking.slot_date.isoformat(),
            start_time=booking.start_time,
            end_time=booking.end_time,
            status=booking.status,
        )
    finally:
        db.close()


@router.get("/{token}")
def get_booking(token: str):
    db = SessionLocal()
    try:
        booking = (
            db.query(DemoBooking)
            .filter(DemoBooking.booking_token == token)
            .first()
        )
        if not booking:
            raise HTTPException(status_code=404, detail="Booking not found")

        return {
            "id": str(booking.id),
            "slot_date": booking.slot_date.isoformat(),
            "start_time": booking.start_time,
            "end_time": booking.end_time,
            "customer_name": booking.customer_name,
            "customer_email": booking.customer_email,
            "customer_phone": booking.customer_phone,
            "notes": booking.notes,
            "status": booking.status,
            "created_at": booking.created_at.isoformat() if booking.created_at else None,
        }
    finally:
        db.close()


@router.post("/{token}/cancel")
def cancel_booking(token: str):
    db = SessionLocal()
    try:
        booking = (
            db.query(DemoBooking)
            .filter(DemoBooking.booking_token == token)
            .first()
        )
        if not booking or booking.status != "confirmed":
            raise HTTPException(status_code=404, detail="Active booking not found")

        booking.status = "cancelled"
        booking.updated_at = datetime.utcnow()
        db.commit()

        return {"status": "cancelled", "id": str(booking.id)}
    finally:
        db.close()