"""External Exam Board Management Models"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey, UniqueConstraint
from typing import Optional, List
from datetime import datetime
from enum import Enum
import uuid


class ExamRegistrationStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    CONFIRMED = "confirmed"
    INDEX_ISSUED = "index_issued"
    RESULTS_RECEIVED = "results_received"
    CANCELLED = "cancelled"


class ExamBoardRegistration(SQLModel, table=True):
    """A student's registration for an external exam board sitting (e.g. BECE, WASSCE)"""
    __tablename__ = "exam_board_registrations"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)

    exam_name: str
    exam_year: str
    index_number: Optional[str] = Field(default=None, index=True)
    subjects_registered: Optional[str] = None

    registration_status: ExamRegistrationStatus = ExamRegistrationStatus.PENDING
    registration_fee_amount: Optional[float] = None
    registration_fee_paid: bool = False
    fee_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("fees.id", ondelete="SET NULL"), index=True)
    )

    exam_center: Optional[str] = None
    notes: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ExamBoardRegistrationCreate(SQLModel):
    student_id: str
    exam_name: str
    exam_year: str
    index_number: Optional[str] = None
    subject_ids: List[str] = []
    registration_status: ExamRegistrationStatus = ExamRegistrationStatus.PENDING
    registration_fee_amount: Optional[float] = None
    exam_center: Optional[str] = None
    notes: Optional[str] = None


class ExamBoardRegistrationUpdate(SQLModel):
    exam_name: Optional[str] = None
    exam_year: Optional[str] = None
    index_number: Optional[str] = None
    subject_ids: Optional[List[str]] = None
    registration_status: Optional[ExamRegistrationStatus] = None
    registration_fee_amount: Optional[float] = None
    exam_center: Optional[str] = None
    notes: Optional[str] = None


class BulkExamBoardRegistrationCreate(SQLModel):
    student_ids: List[str]
    exam_name: str
    exam_year: str
    subject_ids: List[str] = []
    registration_fee_amount: Optional[float] = None
    exam_center: Optional[str] = None
    notes: Optional[str] = None


class ExamBoardFeePayment(SQLModel):
    amount: Optional[float] = None  # defaults to the full outstanding balance
    payment_method: str = "cash"


class BulkIndexNumberImport(SQLModel):
    exam_name: str
    exam_year: str
    csv_text: str  # "student_id,index_number" per line; header row optional


class ExamBoardRegistrationSubject(SQLModel, table=True):
    """Links a registration to a real Subject the student is registered for,
    rather than the free-text subjects_registered label (kept as a derived
    display string for the hall ticket template)."""
    __tablename__ = "exam_board_registration_subjects"
    __table_args__ = (UniqueConstraint("registration_id", "subject_id", name="uq_examboard_reg_subject"),)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    registration_id: str = Field(
        sa_column=Column(String, ForeignKey("exam_board_registrations.id", ondelete="CASCADE"), index=True)
    )
    subject_id: str = Field(index=True)


class ExamBoardResult(SQLModel, table=True):
    """A per-subject result received back from the exam board for one
    registration — the piece that was previously entirely absent."""
    __tablename__ = "exam_board_results"
    __table_args__ = (UniqueConstraint("registration_id", "subject_id", name="uq_examboard_result_subject"),)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    registration_id: str = Field(
        sa_column=Column(String, ForeignKey("exam_board_registrations.id", ondelete="CASCADE"), index=True)
    )
    subject_id: str = Field(index=True)
    grade: Optional[str] = None
    score: Optional[float] = None
    remarks: Optional[str] = None
    recorded_by: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ExamBoardResultEntry(SQLModel):
    subject_id: str
    grade: Optional[str] = None
    score: Optional[float] = None
    remarks: Optional[str] = None


class ExamBoardResultsSubmit(SQLModel):
    results: List[ExamBoardResultEntry]


class BulkResultsImport(SQLModel):
    exam_name: str
    exam_year: str
    csv_text: str  # "student_id,subject_code,grade,score" per line; header row optional


class ExamSeatingAssignment(SQLModel, table=True):
    """A seat assignment for one exam registration"""
    __tablename__ = "exam_seating_assignments"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    registration_id: str = Field(
        sa_column=Column(String, ForeignKey("exam_board_registrations.id", ondelete="CASCADE"), index=True, unique=True)
    )

    room: str
    seat_number: str

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ExamSeatingAssignmentCreate(SQLModel):
    registration_id: str
    room: str
    seat_number: str


class ExamSeatingAssignmentUpdate(SQLModel):
    room: Optional[str] = None
    seat_number: Optional[str] = None


class InvigilationDuty(SQLModel, table=True):
    """A staff member's invigilation assignment for an exam sitting"""
    __tablename__ = "invigilation_duties"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)

    exam_name: str
    exam_year: str
    room: str
    staff_id: str = Field(index=True)
    duty_date: str
    notes: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)


class InvigilationDutyCreate(SQLModel):
    exam_name: str
    exam_year: str
    room: str
    staff_id: str
    duty_date: str
    notes: Optional[str] = None


class InvigilationDutyUpdate(SQLModel):
    room: Optional[str] = None
    duty_date: Optional[str] = None
    notes: Optional[str] = None
