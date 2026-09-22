"""Admissions / Enrollment CRM Models"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey
from typing import Optional
from datetime import datetime
from enum import Enum
import uuid

from models.student import Gender


class ApplicationStatus(str, Enum):
    INQUIRY = "inquiry"
    APPLIED = "applied"
    INTERVIEW_SCHEDULED = "interview_scheduled"
    ENTRANCE_TEST_SCHEDULED = "entrance_test_scheduled"
    ENTRANCE_TEST_COMPLETED = "entrance_test_completed"
    OFFERED = "offered"
    ENROLLED = "enrolled"
    REJECTED = "rejected"
    WAITLISTED = "waitlisted"
    WITHDRAWN = "withdrawn"


class RejectionReasonCode(str, Enum):
    """Validated at the API layer only (ApplicantStageChange.reason_code) —
    Applicant.rejection_reason_code is a plain string column, deliberately
    NOT typed as this Enum, so it never becomes a native Postgres enum
    requiring its own ALTER TYPE migration for every future code added."""
    ACADEMIC_REQUIREMENTS_NOT_MET = "academic_requirements_not_met"
    CLASS_FULL = "class_full"
    INCOMPLETE_DOCUMENTATION = "incomplete_documentation"
    FAILED_ENTRANCE_EXAM = "failed_entrance_exam"
    FAILED_INTERVIEW = "failed_interview"
    AGE_INELIGIBLE = "age_ineligible"
    OTHER = "other"


class WithdrawalReasonCode(str, Enum):
    CHOSE_ANOTHER_SCHOOL = "chose_another_school"
    FINANCIAL = "financial"
    RELOCATION = "relocation"
    DECLINED_OFFER = "declined_offer"
    NO_LONGER_INTERESTED = "no_longer_interested"
    OTHER = "other"


class Applicant(SQLModel, table=True):
    """A prospective student, tracked from first inquiry through enrollment"""
    __tablename__ = "applicants"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)

    first_name: str
    last_name: str
    other_names: Optional[str] = None
    date_of_birth: str
    gender: Gender

    guardian_name: str
    guardian_relationship: str
    guardian_phone: str
    guardian_email: Optional[str] = None

    applying_for_class_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("classes.id", ondelete="SET NULL"), index=True)
    )
    applying_for_term_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("academic_terms.id", ondelete="SET NULL"), index=True)
    )

    status: ApplicationStatus = ApplicationStatus.INQUIRY
    entrance_test_date: Optional[str] = None
    entrance_test_score: Optional[float] = None

    application_fee_amount: Optional[float] = None
    application_fee_paid: bool = False
    rejection_reason: Optional[str] = None
    rejection_reason_code: Optional[str] = None
    withdrawal_reason: Optional[str] = None
    withdrawal_reason_code: Optional[str] = None
    waitlist_rank: Optional[int] = None

    converted_student_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("students.id", ondelete="SET NULL"), index=True)
    )

    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ApplicantCreate(SQLModel):
    first_name: str
    last_name: str
    other_names: Optional[str] = None
    date_of_birth: str
    gender: Gender
    guardian_name: str
    guardian_relationship: str
    guardian_phone: str
    guardian_email: Optional[str] = None
    applying_for_class_id: Optional[str] = None
    applying_for_term_id: Optional[str] = None
    entrance_test_date: Optional[str] = None
    application_fee_amount: Optional[float] = None
    notes: Optional[str] = None


class ApplicantUpdate(SQLModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    other_names: Optional[str] = None
    date_of_birth: Optional[str] = None
    gender: Optional[Gender] = None
    guardian_name: Optional[str] = None
    guardian_relationship: Optional[str] = None
    guardian_phone: Optional[str] = None
    guardian_email: Optional[str] = None
    applying_for_class_id: Optional[str] = None
    applying_for_term_id: Optional[str] = None
    status: Optional[ApplicationStatus] = None
    entrance_test_date: Optional[str] = None
    entrance_test_score: Optional[float] = None
    application_fee_amount: Optional[float] = None
    application_fee_paid: Optional[bool] = None
    rejection_reason: Optional[str] = None
    rejection_reason_code: Optional[str] = None
    withdrawal_reason: Optional[str] = None
    withdrawal_reason_code: Optional[str] = None
    waitlist_rank: Optional[int] = None
    notes: Optional[str] = None


class ApplicantConvertRequest(SQLModel):
    class_id: str
    admission_date: str
    # Optional, same precedence as StudentCreate.campus_id — an unscoped
    # admin may pick one; a campus-scoped admin's own campus always wins
    # regardless of this value (see dependencies.py::resolve_write_campus_id).
    campus_id: Optional[str] = None


class ApplicantDocumentType(str, Enum):
    BIRTH_CERTIFICATE = "birth_certificate"
    PREVIOUS_REPORT_CARD = "previous_report_card"
    PASSPORT_PHOTO = "passport_photo"
    OTHER = "other"


class ApplicantDocument(SQLModel, table=True):
    """A file the applicant/guardian attached to a public application —
    birth certificate, previous report card, passport photo, etc. Uploaded
    separately from the apply submission itself (see routers/public_admissions.py)
    so a failed/retried upload never risks the application record."""
    __tablename__ = "applicant_documents"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    applicant_id: str = Field(
        sa_column=Column(String, ForeignKey("applicants.id", ondelete="CASCADE"), index=True)
    )
    school_id: str = Field(index=True)
    document_type: ApplicantDocumentType
    file_path: str  # e.g. /uploads/admissions/{school_id}/{applicant_id}/{filename}
    original_filename: str
    content_type: str
    file_size: int
    verification_status: str = "pending"
    verification_notes: Optional[str] = None
    verified_by: Optional[str] = None
    verified_at: Optional[datetime] = None
    uploaded_at: datetime = Field(default_factory=datetime.utcnow)


class PublicApplicantCreate(SQLModel):
    """Submitted from the unauthenticated public apply page — a deliberately
    smaller field set than ApplicantCreate: no internal class/term IDs (those
    are admin-pipeline concerns, not something a public form should expose
    or require), no fee/entrance-test fields. `honeypot` must arrive empty;
    a filled value marks the submission as spam."""
    first_name: str
    last_name: str
    other_names: Optional[str] = None
    date_of_birth: str
    gender: Gender
    guardian_name: str
    guardian_relationship: str
    guardian_phone: str
    guardian_email: Optional[str] = None
    notes: Optional[str] = None
    honeypot: Optional[str] = None
