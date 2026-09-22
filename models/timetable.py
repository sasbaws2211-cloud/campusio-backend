"""Timetable and Period models"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey
from typing import Optional
from datetime import datetime
from enum import Enum
import uuid


class DayOfWeek(str, Enum):
    MONDAY = "monday"
    TUESDAY = "tuesday"
    WEDNESDAY = "wednesday"
    THURSDAY = "thursday"
    FRIDAY = "friday"


class PeriodType(str, Enum):
    LESSON = "lesson"
    BREAK = "break"
    ASSEMBLY = "assembly"
    LUNCH = "lunch"


class Period(SQLModel, table=True):
    __tablename__ = "periods"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    name: str
    period_number: int
    start_time: str
    end_time: str
    period_type: PeriodType = PeriodType.LESSON
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class PeriodCreate(SQLModel):
    name: str
    period_number: int
    start_time: str
    end_time: str
    period_type: PeriodType = PeriodType.LESSON


class Timetable(SQLModel, table=True):
    __tablename__ = "timetables"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    class_id: str = Field(index=True)
    subject_id: str = Field(index=True)
    teacher_id: str = Field(index=True)
    period_id: str = Field(index=True)
    day_of_week: DayOfWeek
    academic_term_id: str = Field(sa_column=Column(String, ForeignKey("academic_terms.id", ondelete="CASCADE"), index=True))
    room: Optional[str] = None
    # Optional link to a bookable FacilityRoom (models.facilities.FacilityRoom).
    # Deliberately not a hard DB ForeignKey — FacilityBooking.room_id itself has
    # none either; validated at the application layer only, same convention.
    # The free-text `room` field above is untouched historical data.
    facility_room_id: Optional[str] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class TimetableCreate(SQLModel):
    class_id: str
    subject_id: str
    teacher_id: str
    period_id: str
    day_of_week: DayOfWeek
    academic_term_id: str
    room: Optional[str] = None
    facility_room_id: Optional[str] = None
