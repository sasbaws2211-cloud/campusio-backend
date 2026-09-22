"""Sensitive student support records with explicit safeguarding boundaries."""
from datetime import datetime
from typing import Optional
from enum import Enum
import uuid

from sqlmodel import Field, SQLModel
from sqlalchemy import Column, String, ForeignKey


class ConsentStatus(str, Enum):
    PENDING = "pending"
    GRANTED = "granted"
    WITHDRAWN = "withdrawn"
    REFUSED = "refused"


class SENProfile(SQLModel, table=True):
    __tablename__ = "student_sen_profiles"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(sa_column=Column(String, ForeignKey("students.id", ondelete="CASCADE"), index=True))
    needs_category: str
    description: str
    accommodations: Optional[str] = None
    strengths: Optional[str] = None
    external_provider: Optional[str] = None
    review_date: Optional[str] = None
    active: bool = True
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class SENProfileCreate(SQLModel):
    student_id: str
    needs_category: str
    description: str
    accommodations: Optional[str] = None
    strengths: Optional[str] = None
    external_provider: Optional[str] = None
    review_date: Optional[str] = None


class IndividualEducationPlan(SQLModel, table=True):
    __tablename__ = "student_individual_education_plans"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(sa_column=Column(String, ForeignKey("students.id", ondelete="CASCADE"), index=True))
    # The SEN profile this plan implements. Optional — a school can write an
    # IEP before a formal SEN profile is on file — but when set it must
    # belong to the same student (enforced in the router, not the DB, same
    # as every other cross-reference in this module).
    sen_profile_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("student_sen_profiles.id", ondelete="SET NULL"), index=True)
    )
    title: str
    goals: str
    accommodations: Optional[str] = None
    responsible_staff: Optional[str] = None
    start_date: str
    review_date: Optional[str] = None
    status: str = "active"
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class IEPCreate(SQLModel):
    student_id: str
    sen_profile_id: Optional[str] = None
    title: str
    goals: str
    accommodations: Optional[str] = None
    responsible_staff: Optional[str] = None
    start_date: str
    review_date: Optional[str] = None


class IEPReview(SQLModel, table=True):
    """A logged review event for an IEP — "reviewed periodically" made real
    instead of a single review_date field nothing ever appended to."""
    __tablename__ = "student_iep_reviews"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    iep_id: str = Field(sa_column=Column(String, ForeignKey("student_individual_education_plans.id", ondelete="CASCADE"), index=True))
    review_date: str
    progress_summary: str
    goals_met: Optional[bool] = None
    next_review_date: Optional[str] = None
    reviewed_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class IEPReviewCreate(SQLModel):
    review_date: str
    progress_summary: str
    goals_met: Optional[bool] = None
    next_review_date: Optional[str] = None


class CounsellingSession(SQLModel, table=True):
    __tablename__ = "student_counselling_sessions"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(sa_column=Column(String, ForeignKey("students.id", ondelete="CASCADE"), index=True))
    session_date: str
    counsellor_id: str
    presenting_issue: Optional[str] = None
    confidential_notes: str
    follow_up_date: Optional[str] = None
    risk_level: str = "low"
    created_at: datetime = Field(default_factory=datetime.utcnow)


class CounsellingSessionCreate(SQLModel):
    student_id: str
    session_date: str
    presenting_issue: Optional[str] = None
    confidential_notes: str
    follow_up_date: Optional[str] = None
    risk_level: str = "low"


class BehaviourSupportPlan(SQLModel, table=True):
    __tablename__ = "student_behaviour_support_plans"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(sa_column=Column(String, ForeignKey("students.id", ondelete="CASCADE"), index=True))
    title: str
    target_behaviours: str
    triggers: Optional[str] = None
    prevention_strategies: Optional[str] = None
    response_strategies: Optional[str] = None
    review_date: Optional[str] = None
    status: str = "active"
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class BehaviourSupportPlanCreate(SQLModel):
    student_id: str
    title: str
    target_behaviours: str
    triggers: Optional[str] = None
    prevention_strategies: Optional[str] = None
    response_strategies: Optional[str] = None
    review_date: Optional[str] = None


class SupportEscalation(SQLModel, table=True):
    __tablename__ = "student_support_escalations"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    case_id: str = Field(sa_column=Column(String, ForeignKey("student_support_cases.id", ondelete="CASCADE"), index=True))
    from_severity: str
    to_severity: str
    reason: str
    referred_to: Optional[str] = None
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SupportEscalationCreate(SQLModel):
    case_id: str
    to_severity: str
    reason: str
    referred_to: Optional[str] = None


class ParentConsent(SQLModel, table=True):
    __tablename__ = "student_parent_consents"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(sa_column=Column(String, ForeignKey("students.id", ondelete="CASCADE"), index=True))
    parent_id: Optional[str] = Field(default=None, index=True)
    consent_type: str
    status: ConsentStatus = ConsentStatus.PENDING
    evidence_document_id: Optional[str] = None
    notes: Optional[str] = None
    requested_by: str
    decided_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ParentConsentCreate(SQLModel):
    student_id: str
    parent_id: Optional[str] = None
    consent_type: str
    evidence_document_id: Optional[str] = None
    notes: Optional[str] = None


class ParentConsentDecision(SQLModel):
    status: ConsentStatus
    notes: Optional[str] = None


class SafeguardingReferralStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    CLOSED = "closed"


class SafeguardingReferral(SQLModel, table=True):
    """A logged referral of a safeguarding case to an external statutory
    body (police, social services, a local safeguarding board, ...) — the
    "statutory reporting workflow" a safeguarding case_type previously had
    no structured way to record. One case can have several referrals over
    time (different agencies, a follow-up report), so this is a log, same
    shape as SupportEscalation, not a single field on the case."""
    __tablename__ = "safeguarding_referrals"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    case_id: str = Field(sa_column=Column(String, ForeignKey("student_support_cases.id", ondelete="CASCADE"), index=True))
    agency_name: str
    referral_date: str
    reference_number: Optional[str] = None
    status: SafeguardingReferralStatus = SafeguardingReferralStatus.PENDING
    notes: Optional[str] = None
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class SafeguardingReferralCreate(SQLModel):
    case_id: str
    agency_name: str
    referral_date: str
    reference_number: Optional[str] = None
    notes: Optional[str] = None


class SafeguardingReferralStatusUpdate(SQLModel):
    status: SafeguardingReferralStatus
    notes: Optional[str] = None