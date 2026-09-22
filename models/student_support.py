"""Confidential student welfare, safeguarding, and intervention records."""
from datetime import datetime
from enum import Enum
from typing import Optional
import uuid

from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey


class SupportCaseStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"


class SupportCaseSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# `case_type` is free text (schools use their own local taxonomy), but the
# literal value "safeguarding" gets a narrower access gate than every other
# case type — see SAFEGUARDING_ROLES in routers/student_support.py and
# routers/student_support_enterprise.py. Shared here (not duplicated in
# each router) because the two files' checks must never drift apart —
# a mismatch would be a real access-control bypass, not just a style issue.
SAFEGUARDING_CASE_TYPE = "safeguarding"


def is_safeguarding_case(case_type: str) -> bool:
    return (case_type or "").strip().lower() == SAFEGUARDING_CASE_TYPE


class StudentSupportCase(SQLModel, table=True):
    __tablename__ = "student_support_cases"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(sa_column=Column(String, ForeignKey("students.id"), index=True))
    case_type: str
    severity: SupportCaseSeverity = SupportCaseSeverity.MEDIUM
    status: SupportCaseStatus = SupportCaseStatus.OPEN
    summary: str
    action_plan: Optional[str] = None
    next_review_date: Optional[str] = None
    assigned_to: Optional[str] = None
    created_by: str
    resolved_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class StudentSupportCaseCreate(SQLModel):
    student_id: str
    case_type: str
    severity: SupportCaseSeverity = SupportCaseSeverity.MEDIUM
    summary: str
    action_plan: Optional[str] = None
    next_review_date: Optional[str] = None
    assigned_to: Optional[str] = None


class StudentSupportCaseUpdate(SQLModel):
    case_type: Optional[str] = None
    severity: Optional[SupportCaseSeverity] = None
    status: Optional[SupportCaseStatus] = None
    summary: Optional[str] = None
    action_plan: Optional[str] = None
    next_review_date: Optional[str] = None
    assigned_to: Optional[str] = None


class InterventionPlan(SQLModel, table=True):
    __tablename__ = "student_intervention_plans"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    case_id: str = Field(sa_column=Column(String, ForeignKey("student_support_cases.id", ondelete="CASCADE"), index=True))
    goal: str
    baseline: Optional[str] = None
    target: Optional[str] = None
    strategy: Optional[str] = None
    owner_id: Optional[str] = None
    review_date: Optional[str] = None
    status: str = "active"
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class InterventionPlanCreate(SQLModel):
    case_id: str
    goal: str
    baseline: Optional[str] = None
    target: Optional[str] = None
    strategy: Optional[str] = None
    owner_id: Optional[str] = None
    review_date: Optional[str] = None


class InterventionProgress(SQLModel, table=True):
    __tablename__ = "student_intervention_progress"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    plan_id: str = Field(sa_column=Column(String, ForeignKey("student_intervention_plans.id", ondelete="CASCADE"), index=True))
    progress_note: str
    progress_percent: Optional[int] = Field(default=None, ge=0, le=100)
    next_action: Optional[str] = None
    recorded_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class InterventionProgressCreate(SQLModel):
    progress_note: str
    progress_percent: Optional[int] = Field(default=None, ge=0, le=100)
    next_action: Optional[str] = None