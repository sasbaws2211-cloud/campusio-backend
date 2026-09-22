"""Staff recruitment, hiring, offers, and onboarding records."""
from datetime import datetime
from enum import Enum
from typing import Optional
import uuid

from sqlmodel import Field, SQLModel
from sqlalchemy import Column, String, ForeignKey


class VacancyStatus(str, Enum):
    DRAFT = "draft"
    OPEN = "open"
    ON_HOLD = "on_hold"
    FILLED = "filled"
    CLOSED = "closed"


class StaffApplicantStatus(str, Enum):
    APPLIED = "applied"
    SCREENING = "screening"
    INTERVIEW = "interview"
    OFFERED = "offered"
    HIRED = "hired"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class InterviewResult(str, Enum):
    PENDING = "pending"
    PASS = "pass"
    FAIL = "fail"
    HOLD = "hold"


class OfferStatus(str, Enum):
    DRAFT = "draft"
    SENT = "sent"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"


class Vacancy(SQLModel, table=True):
    __tablename__ = "hr_vacancies"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    title: str
    department: Optional[str] = None
    description: Optional[str] = None
    employment_type: str = "full_time"
    positions: int = 1
    closing_date: Optional[str] = None
    status: VacancyStatus = VacancyStatus.DRAFT
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class VacancyCreate(SQLModel):
    title: str
    department: Optional[str] = None
    description: Optional[str] = None
    employment_type: str = "full_time"
    positions: int = 1
    closing_date: Optional[str] = None
    status: VacancyStatus = VacancyStatus.DRAFT


class VacancyUpdate(SQLModel):
    title: Optional[str] = None
    department: Optional[str] = None
    description: Optional[str] = None
    employment_type: Optional[str] = None
    positions: Optional[int] = None
    closing_date: Optional[str] = None
    status: Optional[VacancyStatus] = None


class StaffApplicant(SQLModel, table=True):
    __tablename__ = "hr_staff_applicants"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    vacancy_id: Optional[str] = Field(default=None, sa_column=Column(String, ForeignKey("hr_vacancies.id", ondelete="SET NULL"), index=True))
    first_name: str
    last_name: str
    email: str
    phone: Optional[str] = None
    qualification: Optional[str] = None
    resume_url: Optional[str] = None
    notes: Optional[str] = None
    status: StaffApplicantStatus = StaffApplicantStatus.APPLIED
    hired_staff_id: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class StaffApplicantCreate(SQLModel):
    vacancy_id: Optional[str] = None
    first_name: str
    last_name: str
    email: str
    phone: Optional[str] = None
    qualification: Optional[str] = None
    resume_url: Optional[str] = None
    notes: Optional[str] = None


class ApplicantStatusUpdate(SQLModel):
    status: StaffApplicantStatus
    notes: Optional[str] = None


class Interview(SQLModel, table=True):
    __tablename__ = "hr_interviews"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    applicant_id: str = Field(sa_column=Column(String, ForeignKey("hr_staff_applicants.id", ondelete="CASCADE"), index=True))
    scheduled_at: str
    panel_members: Optional[str] = None
    score: Optional[float] = None
    feedback: Optional[str] = None
    result: InterviewResult = InterviewResult.PENDING
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class InterviewCreate(SQLModel):
    applicant_id: str
    scheduled_at: str
    panel_members: Optional[str] = None


class InterviewResultUpdate(SQLModel):
    score: Optional[float] = None
    feedback: Optional[str] = None
    result: InterviewResult


class OfferLetter(SQLModel, table=True):
    __tablename__ = "hr_offer_letters"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    applicant_id: str = Field(sa_column=Column(String, ForeignKey("hr_staff_applicants.id", ondelete="CASCADE"), index=True))
    position: str
    salary: Optional[float] = None
    start_date: Optional[str] = None
    expiry_date: Optional[str] = None
    terms: Optional[str] = None
    status: OfferStatus = OfferStatus.DRAFT
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class OfferCreate(SQLModel):
    applicant_id: str
    position: str
    salary: Optional[float] = None
    start_date: Optional[str] = None
    expiry_date: Optional[str] = None
    terms: Optional[str] = None


class OfferStatusUpdate(SQLModel):
    status: OfferStatus