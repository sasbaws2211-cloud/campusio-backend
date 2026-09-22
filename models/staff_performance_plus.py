"""Individual staff goals (OKRs), 360-degree peer feedback, and Performance
Improvement Plans — extensions to the review-cycle system in models/hr.py's
StaffPerformanceReview, which stays as the review record of record. All
three link to a review_id optionally: a goal/PIP can exist standalone
(set at any time) or be tied to a specific review cycle.
"""
from datetime import datetime
from typing import Optional
import uuid

from sqlmodel import Field, SQLModel
from sqlalchemy import Column, String, ForeignKey


class StaffGoal(SQLModel, table=True):
    """Individual staff OKR — same shape as the org-level StrategicGoal,
    scoped to one staff member instead of a whole school."""
    __tablename__ = "hr_staff_goals"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    staff_id: str = Field(sa_column=Column(String, ForeignKey("staff.id", ondelete="CASCADE"), index=True))
    review_id: Optional[str] = Field(default=None, index=True)  # StaffPerformanceReview.id, if tied to a cycle
    title: str
    description: Optional[str] = None
    target_metric: Optional[str] = None
    target_value: Optional[float] = None
    current_value: Optional[float] = None
    unit: Optional[str] = None
    status: str = "not_started"  # not_started/on_track/at_risk/achieved/missed
    due_date: Optional[str] = None
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class StaffGoalCreate(SQLModel):
    staff_id: str
    review_id: Optional[str] = None
    title: str
    description: Optional[str] = None
    target_metric: Optional[str] = None
    target_value: Optional[float] = None
    current_value: Optional[float] = None
    unit: Optional[str] = None
    due_date: Optional[str] = None


class StaffGoalProgressUpdate(SQLModel):
    current_value: Optional[float] = None
    status: Optional[str] = None


class PerformanceFeedbackRequest(SQLModel, table=True):
    """One outstanding ask: "please give feedback on this staff member for
    this review cycle." feedback_giver_staff_id is who was asked;
    PerformanceFeedback below is their actual answer, filled in once
    submitted."""
    __tablename__ = "hr_feedback_requests"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    review_id: str = Field(sa_column=Column(String, ForeignKey("staff_performance_reviews.id", ondelete="CASCADE"), index=True))
    subject_staff_id: str = Field(index=True)  # who this feedback is ABOUT
    feedback_giver_staff_id: str = Field(index=True)  # who was asked to give it
    relationship: str  # "peer" / "subordinate" / "manager" / "self"
    requested_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class PerformanceFeedbackRequestCreate(SQLModel):
    review_id: str
    feedback_giver_staff_ids: list[str]
    relationship: str


class PerformanceFeedback(SQLModel, table=True):
    __tablename__ = "hr_performance_feedback"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    request_id: str = Field(sa_column=Column(String, ForeignKey("hr_feedback_requests.id", ondelete="CASCADE"), index=True))
    rating: Optional[int] = None  # 1-5
    strengths: Optional[str] = None
    areas_for_improvement: Optional[str] = None
    comments: Optional[str] = None
    submitted_at: datetime = Field(default_factory=datetime.utcnow)


class PerformanceFeedbackSubmit(SQLModel):
    rating: Optional[int] = None
    strengths: Optional[str] = None
    areas_for_improvement: Optional[str] = None
    comments: Optional[str] = None


class PerformanceImprovementPlan(SQLModel, table=True):
    __tablename__ = "hr_performance_improvement_plans"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    staff_id: str = Field(sa_column=Column(String, ForeignKey("staff.id", ondelete="CASCADE"), index=True))
    review_id: Optional[str] = Field(default=None, index=True)
    reason: str
    goals: str
    start_date: str
    end_date: str
    status: str = Field(default="active", index=True)  # active/completed/escalated/closed
    check_in_notes: Optional[str] = None
    outcome: Optional[str] = None
    created_by: str
    closed_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class PerformanceImprovementPlanCreate(SQLModel):
    staff_id: str
    review_id: Optional[str] = None
    reason: str
    goals: str
    start_date: str
    end_date: str


class PerformanceImprovementPlanCheckIn(SQLModel):
    check_in_notes: str


class PerformanceImprovementPlanClose(SQLModel):
    status: str  # completed/escalated/closed
    outcome: Optional[str] = None
