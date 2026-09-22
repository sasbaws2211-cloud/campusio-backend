"""Human-resources records beyond payroll."""
from datetime import datetime
from enum import Enum
from typing import List, Optional
import uuid

from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey


class ReviewStatus(str, Enum):
    DRAFT = "draft"
    SELF_ASSESSED = "self_assessed"  # staff has submitted their own input; manager review still pending
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"


class StaffPerformanceReview(SQLModel, table=True):
    __tablename__ = "staff_performance_reviews"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    staff_id: str = Field(sa_column=Column(String, ForeignKey("staff.id"), index=True))
    review_period: str
    status: ReviewStatus = ReviewStatus.DRAFT

    # Staff's own input — set once, via the dedicated self-assessment
    # endpoint, by the staff member the review is about (never by their
    # manager). Optional: a manager can submit without one ever being filed.
    self_rating: Optional[int] = Field(default=None, ge=1, le=5)
    self_comments: Optional[str] = None
    self_submitted_at: Optional[datetime] = None

    # Manager's assessment — the review's primary content.
    overall_rating: Optional[int] = Field(default=None, ge=1, le=5)
    strengths: Optional[str] = None
    development_goals: Optional[str] = None
    training_needs: Optional[str] = None
    reviewer_id: str
    submitted_at: Optional[datetime] = None
    acknowledged_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class StaffPerformanceReviewCreate(SQLModel):
    staff_id: str
    review_period: str
    overall_rating: Optional[int] = Field(default=None, ge=1, le=5)
    strengths: Optional[str] = None
    development_goals: Optional[str] = None
    training_needs: Optional[str] = None


class StaffPerformanceReviewUpdate(SQLModel):
    review_period: Optional[str] = None
    overall_rating: Optional[int] = Field(default=None, ge=1, le=5)
    strengths: Optional[str] = None
    development_goals: Optional[str] = None
    training_needs: Optional[str] = None


class SelfAssessmentSubmit(SQLModel):
    self_rating: Optional[int] = Field(default=None, ge=1, le=5)
    self_comments: Optional[str] = None


class BulkLaunchReviewsRequest(SQLModel):
    review_period: str
    staff_ids: List[str]