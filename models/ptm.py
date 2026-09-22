"""Parent-teacher meeting scheduling. A teacher publishes open time slots;
a parent books one of their own children into an open slot. Booking a slot
flips it to BOOKED so no one else can take it; cancelling (by either side)
flips it back to OPEN so it can be rebooked, and the cancelled PTMBooking row
stays as history rather than being deleted.
"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey
from typing import Optional
from datetime import datetime
from enum import Enum
import uuid


class PTMSlotStatus(str, Enum):
    OPEN = "open"
    BOOKED = "booked"
    CANCELLED = "cancelled"  # teacher withdrew an unbooked slot


class PTMBookingStatus(str, Enum):
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class PTMSlot(SQLModel, table=True):
    __tablename__ = "ptm_slots"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    teacher_id: str = Field(index=True)
    date: str  # "YYYY-MM-DD"
    start_time: str  # "HH:MM"
    end_time: str
    location: Optional[str] = None
    status: PTMSlotStatus = PTMSlotStatus.OPEN
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class PTMBooking(SQLModel, table=True):
    __tablename__ = "ptm_bookings"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    slot_id: str = Field(sa_column=Column(String, ForeignKey("ptm_slots.id", ondelete="CASCADE"), nullable=False, index=True))
    teacher_id: str = Field(index=True)
    parent_id: str = Field(index=True)
    student_id: str = Field(index=True)
    purpose: Optional[str] = None
    status: PTMBookingStatus = PTMBookingStatus.CONFIRMED
    cancelled_by: Optional[str] = None  # "parent" or "teacher"
    cancellation_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    cancelled_at: Optional[datetime] = None


class PTMSlotCreate(SQLModel):
    date: str
    start_time: str
    end_time: str
    location: Optional[str] = None


class PTMBookingCreate(SQLModel):
    slot_id: str
    student_id: str
    purpose: Optional[str] = None


class PTMCancelRequest(SQLModel):
    reason: Optional[str] = None
