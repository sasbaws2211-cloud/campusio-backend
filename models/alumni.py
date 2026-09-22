"""Alumni Management Models"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey
from typing import Optional
from datetime import datetime
from enum import Enum
import uuid


class DonationApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class AlumniRecord(SQLModel, table=True):
    """A former student, or a standalone pre-records alumnus with no linked Student row"""
    __tablename__ = "alumni_records"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)

    student_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("students.id", ondelete="SET NULL"), index=True)
    )

    first_name: str
    last_name: str
    other_names: Optional[str] = None
    graduation_year: str
    last_class_completed: Optional[str] = None

    current_occupation: Optional[str] = None
    current_institution: Optional[str] = None

    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None

    opt_in_communications: bool = True

    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AlumniRecordCreate(SQLModel):
    student_id: Optional[str] = None
    first_name: str
    last_name: str
    other_names: Optional[str] = None
    graduation_year: str
    last_class_completed: Optional[str] = None
    current_occupation: Optional[str] = None
    current_institution: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    opt_in_communications: bool = True
    notes: Optional[str] = None


class AlumniRecordUpdate(SQLModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    other_names: Optional[str] = None
    graduation_year: Optional[str] = None
    last_class_completed: Optional[str] = None
    current_occupation: Optional[str] = None
    current_institution: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    opt_in_communications: Optional[bool] = None
    notes: Optional[str] = None


class AlumniOutreachRequest(SQLModel):
    message: str
    subject: Optional[str] = None
    graduation_year: Optional[str] = None


class AlumniDonation(SQLModel, table=True):
    """A single donation/pledge logged against an alumnus"""
    __tablename__ = "alumni_donations"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    alumni_id: str = Field(sa_column=Column(String, ForeignKey("alumni_records.id", ondelete="CASCADE"), index=True))

    amount: float
    donation_date: str
    campaign: Optional[str] = None
    payment_method: Optional[str] = None
    notes: Optional[str] = None

    # User.id of whoever logged the donation (server-set — not accepted from
    # AlumniDonationCreate). Needed for the maker-checker segregation-of-duties
    # check below.
    created_by: Optional[str] = None

    # Maker-checker (School.require_maker_checker, off by default): when
    # enabled, a donation is logged PENDING and needs sign-off from a
    # different staff member via routers/alumni.py::approve_donation before
    # it counts toward the fundraising totals in GET /donations/summary
    # (mirrors routers/discipline.py gating its demerit tally the same way).
    # When disabled (the default), donations are auto-approved at creation —
    # identical to this module's original behavior.
    approval_status: DonationApprovalStatus = DonationApprovalStatus.APPROVED
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)


class AlumniDonationCreate(SQLModel):
    amount: float
    donation_date: str
    campaign: Optional[str] = None
    payment_method: Optional[str] = None
    notes: Optional[str] = None


class RejectDonationRequest(SQLModel):
    rejection_reason: Optional[str] = None
