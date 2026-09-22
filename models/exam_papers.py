"""Exam paper / question-bank management and paper moderation.

Distinct from models/exam.py (scheduling/seating/invigilation) — this
covers the actual exam PAPER content: a reusable question bank, papers
assembled from bank questions (or a single uploaded document via
file_url), and a maker-checker moderation workflow before a paper counts
as approved, mirroring the approval pattern already used for
teacher-created suspensions in models/discipline.py (ActionApprovalStatus).

Status/category fields are deliberately plain `str` (validated by a
Python Enum only at the API layer, never typed onto the table column) —
see models/parent_requests.py for why this codebase avoids the
native-Postgres-enum trap on new tables.
"""
from datetime import datetime
from enum import Enum
from typing import Optional
import uuid

from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey


class QuestionType(str, Enum):
    MCQ = "mcq"
    SHORT_ANSWER = "short_answer"
    ESSAY = "essay"
    PRACTICAL = "practical"


class QuestionBankItem(SQLModel, table=True):
    """A single reusable exam question, tagged by subject/topic/difficulty
    so it can be pulled into multiple papers across terms/years."""
    __tablename__ = "question_bank_items"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    subject_id: str = Field(index=True)
    topic: Optional[str] = None
    question_type: str = QuestionType.SHORT_ANSWER.value
    question_text: str
    options: Optional[str] = None  # JSON-encoded list of choices, MCQ only
    answer_key: Optional[str] = None  # marking reference — never shown to students
    marks: float = 1.0
    difficulty: str = "medium"  # easy, medium, hard — free text, no fixed band
    is_active: bool = True
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class QuestionBankItemCreate(SQLModel):
    subject_id: str
    topic: Optional[str] = None
    question_type: QuestionType = QuestionType.SHORT_ANSWER
    question_text: str
    options: Optional[str] = None
    answer_key: Optional[str] = None
    marks: float = 1.0
    difficulty: str = "medium"


class QuestionBankItemUpdate(SQLModel):
    topic: Optional[str] = None
    question_type: Optional[QuestionType] = None
    question_text: Optional[str] = None
    options: Optional[str] = None
    answer_key: Optional[str] = None
    marks: Optional[float] = None
    difficulty: Optional[str] = None
    is_active: Optional[bool] = None


class ExamPaperStatus(str, Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"


class ExamPaper(SQLModel, table=True):
    """A specific exam paper — either assembled from QuestionBankItem rows
    (via ExamPaperQuestion) or a single uploaded document (file_url).
    Starts DRAFT, moves to SUBMITTED when the setter is done, and only a
    SCHOOL_ADMIN/SUPER_ADMIN can APPROVE or REJECT it — see
    routers/exam_papers.py. Optionally linked to a scheduled sitting."""
    __tablename__ = "exam_papers"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    subject_id: str = Field(index=True)
    exam_schedule_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("exam_schedules.id", ondelete="SET NULL"), index=True),
    )
    title: str
    instructions: Optional[str] = None
    duration_minutes: Optional[int] = None
    total_marks: float = 0
    file_url: Optional[str] = None  # set instead of question-bank assembly, for a scanned/uploaded paper
    status: str = ExamPaperStatus.DRAFT.value
    created_by: str
    submitted_at: Optional[datetime] = None
    moderated_by: Optional[str] = None
    moderated_at: Optional[datetime] = None
    moderation_notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ExamPaperCreate(SQLModel):
    subject_id: str
    exam_schedule_id: Optional[str] = None
    title: str
    instructions: Optional[str] = None
    duration_minutes: Optional[int] = None
    file_url: Optional[str] = None


class ExamPaperUpdate(SQLModel):
    title: Optional[str] = None
    instructions: Optional[str] = None
    duration_minutes: Optional[int] = None
    file_url: Optional[str] = None
    exam_schedule_id: Optional[str] = None


class ExamPaperModerationDecision(SQLModel):
    notes: Optional[str] = None


class ExamPaperQuestion(SQLModel, table=True):
    """One question placed on one paper, with marks allocated for THIS
    paper (may differ from the bank item's default marks) and display order."""
    __tablename__ = "exam_paper_questions"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    exam_paper_id: str = Field(sa_column=Column(String, ForeignKey("exam_papers.id", ondelete="CASCADE"), index=True))
    question_bank_item_id: str = Field(sa_column=Column(String, ForeignKey("question_bank_items.id", ondelete="CASCADE"), index=True))
    marks_allocated: float
    order_index: int = 0


class ExamPaperQuestionAdd(SQLModel):
    question_bank_item_id: str
    marks_allocated: float
    order_index: int = 0
