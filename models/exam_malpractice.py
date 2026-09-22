"""Academic-integrity / exam malpractice case tracking — distinct from
the general behavioral-incident module in models/discipline.py, which has
no link to a specific exam sitting, no malpractice-specific categories,
and no way to annul a result. A resolved case with a result-annulling
sanction flips ExamComponentMark.annulled for the affected student —
see routers/exam_malpractice.py::update_case.
"""
from datetime import datetime
from enum import Enum
from typing import Optional
import uuid

from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey


class MalpracticeCategory(str, Enum):
    UNAUTHORIZED_MATERIAL = "unauthorized_material"
    COLLUSION = "collusion"
    IMPERSONATION = "impersonation"
    PLAGIARISM = "plagiarism"
    DISRUPTIVE_CONDUCT = "disruptive_conduct"
    OTHER = "other"


class MalpracticeStatus(str, Enum):
    REPORTED = "reported"
    UNDER_INVESTIGATION = "under_investigation"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class MalpracticeSanction(str, Enum):
    NONE = "none"
    WARNING = "warning"
    RESULT_ANNULLED = "result_annulled"
    SUSPENSION = "suspension"
    EXPULSION = "expulsion"
    OTHER = "other"


class MalpracticeCase(SQLModel, table=True):
    __tablename__ = "malpractice_cases"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    exam_schedule_id: str = Field(sa_column=Column(String, ForeignKey("exam_schedules.id", ondelete="CASCADE"), index=True))
    student_id: str = Field(sa_column=Column(String, ForeignKey("students.id", ondelete="CASCADE"), index=True))
    exam_component_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("exam_components.id", ondelete="SET NULL"), index=True),
    )
    reported_by: str = Field(index=True)
    category: str = MalpracticeCategory.OTHER.value
    description: str
    status: str = MalpracticeStatus.REPORTED.value
    investigation_notes: Optional[str] = None
    sanction: Optional[str] = None
    sanction_notes: Optional[str] = None
    resolved_by: Optional[str] = None
    resolved_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class MalpracticeCaseCreate(SQLModel):
    exam_schedule_id: str
    student_id: str
    exam_component_id: Optional[str] = None
    category: MalpracticeCategory = MalpracticeCategory.OTHER
    description: str


class MalpracticeCaseUpdate(SQLModel):
    status: Optional[MalpracticeStatus] = None
    investigation_notes: Optional[str] = None
    sanction: Optional[MalpracticeSanction] = None
    sanction_notes: Optional[str] = None
