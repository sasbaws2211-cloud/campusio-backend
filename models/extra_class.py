from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Optional, List

from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey
from sqlalchemy.types import Enum as SQLEnum

from models.assignment import SubmissionStatus


class ExtraClassStatus(str, Enum):
    DRAFT = "draft"  # teacher still setting it up, not published
    PUBLISHED = "published"  # teacher-published, visible to parents
    CLOSED = "closed"
    CANCELLED = "cancelled"


class EnrollmentStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    ACTIVE = "active"
    COMPLETED = "completed"
    WITHDRAWN = "withdrawn"


class BillingInterval(str, Enum):
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    ONE_TIME = "one_time"


class BillingCycleStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    OVERDUE = "overdue"
    CANCELLED = "cancelled"


class PayoutStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    PAID = "paid"
    REJECTED = "rejected"


# SubmissionStatus is imported from models.assignment above — this used to be a
# separate local enum (draft/submitted/late/graded) that happened to share the
# class name with models.assignment.SubmissionStatus but had different members,
# which was a landmine waiting for something to import the wrong one. Now both
# ExtraClassSubmission and the core Submission share one enum.


class ExtraClass(SQLModel, table=True):
    __tablename__ = "extra_classes"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    teacher_id: str = Field(index=True)
    subject_id: str = Field(index=True)
    class_id: Optional[str] = Field(default=None, index=True)
    title: str
    description: Optional[str] = None
    pricing_type: str = Field(default="hourly")
    price: float = Field(default=0.0)
    billing_interval: BillingInterval = Field(default=BillingInterval.MONTHLY)
    payout_frequency: BillingInterval = Field(default=BillingInterval.MONTHLY)
    session_duration_hours: float = Field(default=1.0)
    frequency_per_week: int = Field(default=1)
    max_students_per_session: int = Field(default=10)
    contact_phone: Optional[str] = None
    contact_email: Optional[str] = None
    meeting_link: Optional[str] = None
    schedule: Optional[str] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    status: ExtraClassStatus = Field(
        default=ExtraClassStatus.DRAFT,
        sa_column=Column(SQLEnum('draft', 'published', 'closed', 'cancelled', name='extraclassstatus', native_enum=False))
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ExtraClassEnrollment(SQLModel, table=True):
    __tablename__ = "extra_class_enrollments"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    extra_class_id: str = Field(index=True)
    student_id: str = Field(index=True)
    parent_id: str = Field(index=True)
    status: EnrollmentStatus = Field(
        default=EnrollmentStatus.PENDING,
        sa_column=Column(SQLEnum('pending', 'approved', 'rejected', 'active', 'completed', 'withdrawn', name='enrollmentstatus', native_enum=False))
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    approved_at: Optional[datetime] = None
    rejected_at: Optional[datetime] = None
    withdrawn_at: Optional[datetime] = None


class ExtraClassSession(SQLModel, table=True):
    __tablename__ = "extra_class_sessions"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    extra_class_id: str = Field(index=True)
    session_date: datetime
    topic: Optional[str] = None
    notes: Optional[str] = None
    attendance_count: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ExtraClassAssignment(SQLModel, table=True):
    __tablename__ = "extra_class_assignments"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    extra_class_id: str = Field(index=True)
    teacher_id: str = Field(index=True)
    title: str
    description: Optional[str] = None
    instructions: Optional[str] = None
    due_date: Optional[datetime] = None
    max_score: float = Field(default=100.0)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ExtraClassSubmission(SQLModel, table=True):
    __tablename__ = "extra_class_submissions"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    extra_class_id: str = Field(index=True)
    assignment_id: str = Field(index=True)
    student_id: str = Field(index=True)
    status: SubmissionStatus = Field(
        default=SubmissionStatus.NOT_SUBMITTED,
        sa_column=Column(SQLEnum('not_submitted', 'submitted', 'graded', 'late', 'excused', name='submissionstatus_extraclass', native_enum=False))
    )
    submission_text: Optional[str] = None
    submission_file_url: Optional[str] = None
    submitted_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ExtraClassGrade(SQLModel, table=True):
    __tablename__ = "extra_class_grades"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    extra_class_id: str = Field(index=True)
    assignment_id: str = Field(index=True)
    submission_id: str = Field(index=True)
    student_id: str = Field(index=True)
    teacher_id: str = Field(index=True)
    score: float = Field(default=0.0)
    feedback: Optional[str] = None
    graded_at: datetime = Field(default_factory=datetime.utcnow)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ExtraClassBillingCycle(SQLModel, table=True):
    __tablename__ = "extra_class_billing_cycles"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    extra_class_id: str = Field(index=True)
    teacher_id: str = Field(index=True)
    enrollment_id: str = Field(index=True)
    parent_id: str = Field(index=True)
    student_id: str = Field(index=True)
    amount: float = Field(default=0.0)
    interval: BillingInterval = Field(default=BillingInterval.MONTHLY)
    next_due_date: Optional[datetime] = None
    status: str = Field(default="pending")
    paid_at: Optional[datetime] = None
    # Set once a paid cycle has been claimed by a TeacherPayoutRequest, so
    # the same revenue can never be counted toward two payout requests.
    # Cleared if that payout request is rejected, releasing the cycle back
    # into the teacher's available balance.
    payout_request_id: Optional[str] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ExtraClassPayment(SQLModel, table=True):
    __tablename__ = "extra_class_payments"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    billing_cycle_id: str = Field(index=True)
    amount: float = Field(default=0.0)
    payment_method: Optional[str] = None
    reference: Optional[str] = None
    status: str = Field(default="paid")
    paid_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class TeacherPayoutRequest(SQLModel, table=True):
    __tablename__ = "teacher_payout_requests"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    teacher_id: str = Field(index=True)
    extra_class_id: str = Field(index=True)
    amount: float = Field(default=0.0)
    payout_period: str = Field(default="monthly")
    status: PayoutStatus = Field(default=PayoutStatus.PENDING)
    requested_at: datetime = Field(default_factory=datetime.utcnow)
    approved_at: Optional[datetime] = None
    paid_at: Optional[datetime] = None
    reference: Optional[str] = None
    notes: Optional[str] = None
    reviewed_by: Optional[str] = None  # User.id of the admin who approved/rejected/paid it
    rejection_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ExtraClassReminderLog(SQLModel, table=True):
    __tablename__ = "extra_class_reminder_logs"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    billing_cycle_id: str = Field(index=True)
    parent_id: str = Field(index=True)
    reminder_type: str = Field(default="due_reminder")
    message: str
    status: str = Field(default="sent")
    sent_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ExtraClassCreate(SQLModel):
    subject_id: str
    class_id: Optional[str] = None
    title: str
    description: Optional[str] = None
    pricing_type: str = "hourly"
    price: float = 0.0
    billing_interval: BillingInterval = BillingInterval.MONTHLY
    payout_frequency: BillingInterval = BillingInterval.MONTHLY
    session_duration_hours: float = 1.0
    frequency_per_week: int = 1
    max_students_per_session: int = 10
    contact_phone: Optional[str] = None
    contact_email: Optional[str] = None
    meeting_link: Optional[str] = None
    schedule: Optional[str] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    status: ExtraClassStatus = ExtraClassStatus.DRAFT


class ExtraClassUpdate(SQLModel):
    title: Optional[str] = None
    description: Optional[str] = None
    pricing_type: Optional[str] = None
    price: Optional[float] = None
    billing_interval: Optional[BillingInterval] = None
    payout_frequency: Optional[BillingInterval] = None
    session_duration_hours: Optional[float] = None
    frequency_per_week: Optional[int] = None
    max_students_per_session: Optional[int] = None
    contact_phone: Optional[str] = None
    contact_email: Optional[str] = None
    meeting_link: Optional[str] = None
    schedule: Optional[str] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    status: Optional[ExtraClassStatus] = None
