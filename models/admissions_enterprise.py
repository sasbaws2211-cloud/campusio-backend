"""Enterprise admissions workflow records."""
from datetime import datetime
from typing import Optional
from enum import Enum
import uuid
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey


class AdmissionOfferStatus(str, Enum):
    DRAFT = "draft"
    SENT = "sent"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"


class AdmissionDepositStatus(str, Enum):
    PENDING = "pending"
    PARTIAL = "partial"
    PAID = "paid"
    WAIVED = "waived"


class ApplicantInterview(SQLModel, table=True):
    __tablename__ = "applicant_interviews"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    applicant_id: str = Field(sa_column=Column(String, ForeignKey("applicants.id", ondelete="CASCADE"), index=True))
    scheduled_at: str
    interviewer: Optional[str] = None
    score: Optional[float] = None
    feedback: Optional[str] = None
    result: str = "pending"
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ApplicantInterviewCreate(SQLModel):
    applicant_id: str
    scheduled_at: str
    interviewer: Optional[str] = None


class ApplicantInterviewUpdate(SQLModel):
    score: Optional[float] = None
    feedback: Optional[str] = None
    result: str


class ApplicantOffer(SQLModel, table=True):
    __tablename__ = "applicant_offers"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    applicant_id: str = Field(sa_column=Column(String, ForeignKey("applicants.id", ondelete="CASCADE"), index=True))
    class_id: Optional[str] = None
    offered_date: str
    expiry_date: Optional[str] = None
    terms: Optional[str] = None
    status: AdmissionOfferStatus = AdmissionOfferStatus.DRAFT
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ApplicantOfferCreate(SQLModel):
    applicant_id: str
    class_id: Optional[str] = None
    offered_date: str
    expiry_date: Optional[str] = None
    terms: Optional[str] = None


class ApplicantOfferStatusUpdate(SQLModel):
    status: AdmissionOfferStatus


class AdmissionDeposit(SQLModel, table=True):
    __tablename__ = "admission_deposits"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    applicant_id: str = Field(sa_column=Column(String, ForeignKey("applicants.id", ondelete="CASCADE"), index=True))
    required_amount: float
    paid_amount: float = 0
    currency: str = "GHS"
    status: AdmissionDepositStatus = AdmissionDepositStatus.PENDING
    due_date: Optional[str] = None
    waived_reason: Optional[str] = None
    recorded_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AdmissionDepositCreate(SQLModel):
    applicant_id: str
    required_amount: float
    due_date: Optional[str] = None


class AdmissionDepositPayment(SQLModel):
    amount: float


class AdmissionDepositWaive(SQLModel):
    reason: Optional[str] = None


class AdmissionDepositOnlinePaymentRequest(SQLModel):
    amount: Optional[float] = None


class ApplicantStageEvent(SQLModel, table=True):
    __tablename__ = "applicant_stage_events"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    applicant_id: str = Field(index=True)
    from_status: Optional[str] = None
    to_status: str
    reason: Optional[str] = None
    reason_code: Optional[str] = None
    changed_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ApplicantStageChange(SQLModel):
    status: str
    reason: Optional[str] = None
    reason_code: Optional[str] = None


class WaitlistReorderRequest(SQLModel):
    new_rank: int


class EntranceExamResult(SQLModel, table=True):
    __tablename__ = "entrance_exam_results"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    applicant_id: str = Field(sa_column=Column(String, ForeignKey("applicants.id", ondelete="CASCADE"), index=True))
    exam_name: str
    exam_date: str
    subject: str
    score: float
    max_score: float = 100
    grade: Optional[str] = None
    notes: Optional[str] = None
    recorded_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class EntranceExamResultCreate(SQLModel):
    applicant_id: str
    exam_name: str
    exam_date: str
    subject: str
    score: float
    max_score: float = 100
    grade: Optional[str] = None
    notes: Optional[str] = None


class ApplicationReview(SQLModel, table=True):
    """A single reviewer's scored rubric pass over an applicant. Multiple
    reviewers may each leave their own row for the same applicant — this is
    additive, not a single mutable verdict — so GET returns every review and
    any aggregation (e.g. an average) is left to the caller."""
    __tablename__ = "application_reviews"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    applicant_id: str = Field(sa_column=Column(String, ForeignKey("applicants.id", ondelete="CASCADE"), index=True))
    reviewer_id: str
    # JSON-encoded list of {"name","weight","score"} — matches
    # routers/custom_reports.py's json.dumps/json.loads convention for
    # freeform filter fields rather than a native Postgres JSON column.
    criteria: str
    total_score: float
    recommendation: Optional[str] = None  # "admit" | "waitlist" | "reject"
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ApplicationReviewCriterion(SQLModel):
    name: str
    weight: float
    score: float


class ApplicationReviewCreate(SQLModel):
    criteria: list[ApplicationReviewCriterion]
    recommendation: Optional[str] = None