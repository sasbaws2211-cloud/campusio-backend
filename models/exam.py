"""Internal Exam Scheduling Models

Distinct from models/exam_board.py, which covers EXTERNAL exam-board
sittings (WAEC/BECE-style) with unconstrained manual seating/invigilation
entry. This covers the school's own internally-run exams (midterms,
finals): a named exam session, a per-class/subject schedule within it with
real room/time conflict detection, auto-generated seating, and invigilator
assignment with double-booking prevention.
"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey
from typing import Optional
from datetime import datetime
from enum import Enum
import uuid


class ExamSessionStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    COMPLETED = "completed"


class ExamSession(SQLModel, table=True):
    """A named internal exam period, e.g. 'Mid-Term Exams — Term 2'. Holds
    the ExamSchedule entries (one per class+subject sitting) within it."""
    __tablename__ = "exam_sessions"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    # None = spans the whole school; set = restricted to one campus, same
    # convention as Student/Staff/Class/FeeStructure/User campus scoping.
    campus_id: Optional[str] = Field(default=None, index=True)
    academic_term_id: str = Field(sa_column=Column(String, ForeignKey("academic_terms.id", ondelete="CASCADE"), index=True))
    name: str
    start_date: str
    end_date: str
    # Plain str (validated by ExamSessionStatus only at the API layer) —
    # this table was never actually migrated before the exam-office build
    # (see alembic/versions/x2d0f4b8c1e5a), so there's no legacy native-enum
    # column to work around; typing it Enum-on-the-column from the start
    # would just recreate the same trap this session hit repeatedly
    # elsewhere. See models/parent_requests.py for the established pattern.
    status: str = ExamSessionStatus.DRAFT.value
    # Result publication gate — component marks for this session are hidden
    # from students/parents (routers/exam_marks.py::_results_visible) until
    # this is true, either by an explicit publish action or automatically
    # once results_release_date passes (services/scheduler.py::run_exam_result_auto_publish).
    results_published: bool = False
    results_release_date: Optional[str] = None
    results_published_by: Optional[str] = None
    results_published_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ExamSessionCreate(SQLModel):
    academic_term_id: str
    campus_id: Optional[str] = None
    name: str
    start_date: str
    end_date: str


class ExamSessionUpdate(SQLModel):
    name: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    status: Optional[ExamSessionStatus] = None


class ExamSessionReleaseDate(SQLModel):
    results_release_date: Optional[str] = None  # "YYYY-MM-DD"; None clears it


class ExamSchedule(SQLModel, table=True):
    """One class+subject's exam sitting within an ExamSession — the actual
    date/time/room slot students and invigilators are booked against."""
    __tablename__ = "exam_schedules"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    exam_session_id: str = Field(sa_column=Column(String, ForeignKey("exam_sessions.id", ondelete="CASCADE"), index=True))
    class_id: str = Field(index=True)
    subject_id: str = Field(index=True)
    exam_date: str
    start_time: str  # "HH:MM", 24-hour — compared lexicographically for overlap checks
    end_time: str
    room: Optional[str] = None
    max_marks: float = 100
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ExamScheduleCreate(SQLModel):
    class_id: str
    subject_id: str
    exam_date: str
    start_time: str
    end_time: str
    room: Optional[str] = None
    max_marks: float = 100


class ExamScheduleUpdate(SQLModel):
    exam_date: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    room: Optional[str] = None
    max_marks: Optional[float] = None


class ExamSeatAssignment(SQLModel, table=True):
    """A student's seat for one ExamSchedule sitting — auto-generated via
    /exams/schedules/{id}/generate-seating, individually overridable after."""
    __tablename__ = "exam_seat_assignments"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    exam_schedule_id: str = Field(sa_column=Column(String, ForeignKey("exam_schedules.id", ondelete="CASCADE"), index=True))
    student_id: str = Field(index=True)
    room: str
    seat_number: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ExamSeatAssignmentUpdate(SQLModel):
    room: Optional[str] = None
    seat_number: Optional[str] = None


class ExamInvigilator(SQLModel, table=True):
    """A staff member assigned to invigilate one ExamSchedule sitting."""
    __tablename__ = "exam_invigilators"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    exam_schedule_id: str = Field(sa_column=Column(String, ForeignKey("exam_schedules.id", ondelete="CASCADE"), index=True))
    staff_id: str = Field(index=True)
    is_chief_invigilator: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ExamInvigilatorCreate(SQLModel):
    staff_id: str
    is_chief_invigilator: bool = False
