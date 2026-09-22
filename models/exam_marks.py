"""Marks capture per exam COMPONENT (Paper 1/2, practical/theory, ...)
tied to a specific ExamSchedule sitting — distinct from the generic
Grade/AssessmentType gradebook in models/grade.py, which has no link back
to a scheduled exam at all. Components let one subject's exam sitting be
split into separately-marked, separately-weighted parts, and give
result-publication (models/exam.py::ExamSession.results_published) and
malpractice annulment (models/exam_malpractice.py) something concrete to
gate/flag.
"""
from datetime import datetime
from typing import List, Optional
import uuid

from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey


class ExamComponent(SQLModel, table=True):
    __tablename__ = "exam_components"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    exam_schedule_id: str = Field(sa_column=Column(String, ForeignKey("exam_schedules.id", ondelete="CASCADE"), index=True))
    exam_paper_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("exam_papers.id", ondelete="SET NULL"), index=True),
    )
    name: str  # "Paper 1", "Practical", "Theory", ...
    max_marks: float = 100
    weight: float = 1.0
    order_index: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ExamComponentCreate(SQLModel):
    name: str
    max_marks: float = 100
    weight: float = 1.0
    order_index: int = 0
    exam_paper_id: Optional[str] = None


class ExamComponentUpdate(SQLModel):
    name: Optional[str] = None
    max_marks: Optional[float] = None
    weight: Optional[float] = None
    order_index: Optional[int] = None


class ExamComponentMark(SQLModel, table=True):
    """One student's score on one component — upserted (create-or-update
    on the same student+component pair) rather than allowing duplicates,
    so a re-mark just overwrites instead of piling up stale rows."""
    __tablename__ = "exam_component_marks"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    exam_component_id: str = Field(sa_column=Column(String, ForeignKey("exam_components.id", ondelete="CASCADE"), index=True))
    student_id: str = Field(index=True)
    score: float
    remarks: Optional[str] = None
    # Set by a resolved malpractice case with a result-annulling sanction —
    # the score is kept for the record but callers should exclude it from
    # any aggregate/average. See routers/exam_malpractice.py.
    annulled: bool = False
    recorded_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ExamComponentMarkUpsert(SQLModel):
    student_id: str
    score: float
    remarks: Optional[str] = None


class BulkExamComponentMarksUpsert(SQLModel):
    marks: List[ExamComponentMarkUpsert]
