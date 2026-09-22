"""Student/parent-initiated exam recheck/remark requests — same
request/review shape as models/parent_requests.py (AbsenceRequest,
DocumentRequest), but tied to a specific ExamComponentMark rather than a
free-floating reason, and snapshotting the original_score so a later
change to the underlying mark doesn't retroactively rewrite what was
actually contested.
"""
from datetime import datetime
from enum import Enum
from typing import Optional
import uuid

from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey


class RemarkRequestStatus(str, Enum):
    PENDING = "pending"
    UNDER_REVIEW = "under_review"
    UPHELD = "upheld"      # original score confirmed correct, no change
    REVISED = "revised"    # score was changed
    REJECTED = "rejected"  # request denied outright (e.g. past the deadline)


class ExamRemarkRequest(SQLModel, table=True):
    __tablename__ = "exam_remark_requests"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(sa_column=Column(String, ForeignKey("students.id", ondelete="CASCADE"), index=True))
    exam_component_id: str = Field(sa_column=Column(String, ForeignKey("exam_components.id", ondelete="CASCADE"), index=True))
    requested_by: str = Field(index=True)
    reason: str
    original_score: float
    revised_score: Optional[float] = None
    status: str = RemarkRequestStatus.PENDING.value
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    review_notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ExamRemarkRequestCreate(SQLModel):
    student_id: str
    exam_component_id: str
    reason: str


class ExamRemarkRequestReview(SQLModel):
    status: RemarkRequestStatus
    revised_score: Optional[float] = None
    review_notes: Optional[str] = None
