"""Staff shift models — per-school, named shifts (e.g. different start times
for teaching vs non-teaching staff) used to resolve PRESENT vs LATE on self
clock-in and to know which staff are expected in on a given day for
auto-absent marking. Staff.shift_id is a plain nullable cross-reference, no
DB FK — consistent with every other staff_attendance/staff reference in this
codebase. No shift-change history is kept; reassigning takes effect
immediately.
"""
from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime
import uuid


class Shift(SQLModel, table=True):
    __tablename__ = "shifts"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    name: str
    start_time: str  # "HH:MM:SS", matches StaffAttendance.check_in format
    end_time: str
    late_grace_minutes: int = Field(default=15)
    is_default: bool = Field(default=False)  # at most one per school — enforced in the service layer
    is_active: bool = Field(default=True)  # soft delete
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ShiftCreate(SQLModel):
    name: str
    start_time: str
    end_time: str
    late_grace_minutes: int = 15


class ShiftUpdate(SQLModel):
    name: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    late_grace_minutes: Optional[int] = None


class ShiftAssign(SQLModel):
    shift_id: Optional[str] = None  # null unassigns


class ShiftBulkAssign(SQLModel):
    staff_ids: list[str]
    shift_id: Optional[str] = None
