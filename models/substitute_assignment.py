"""Substitute-teacher coverage for approved leave requests. A leave approval
by itself doesn't touch the timetable (see leave_request_service.py) — this
model is the admin's record of who covers which of the absent teacher's
timetabled periods while they're away. One row per (leave_request,
timetable_entry) pair; re-assigning a period overwrites the existing row
rather than stacking duplicates.
"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey, UniqueConstraint
from datetime import datetime
import uuid


class SubstituteAssignment(SQLModel, table=True):
    __tablename__ = "substitute_assignments"
    __table_args__ = (
        UniqueConstraint("leave_request_id", "timetable_entry_id", name="uq_substitute_leave_timetable"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    leave_request_id: str = Field(sa_column=Column(String, ForeignKey("leave_requests.id", ondelete="CASCADE"), index=True))
    timetable_entry_id: str = Field(sa_column=Column(String, ForeignKey("timetables.id", ondelete="CASCADE"), index=True))
    original_teacher_id: str = Field(index=True)
    substitute_teacher_id: str = Field(index=True)
    assigned_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SubstituteAssignmentCreate(SQLModel):
    timetable_entry_id: str
    substitute_teacher_id: str


class BulkSubstituteAssignmentCreate(SQLModel):
    substitute_teacher_id: str
