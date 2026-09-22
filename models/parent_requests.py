"""Parent self-service request workflows: absence requests and general
document/certificate requests. Status/category fields are deliberately
plain `str` (validated by a Python Enum only at the API layer, never typed
onto the table column) — this session hit the native-Postgres-enum ALTER
TYPE trap enough times on Python-Enum-typed columns that every new model
from here on just avoids it outright, same as models/leave_request.py and
models/ticket.py already do elsewhere in this codebase.
"""
from datetime import datetime
from enum import Enum
from typing import Optional
import uuid

from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey


class RequestStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class AbsenceRequestType(str, Enum):
    ABSENCE = "absence"        # start_date..end_date, full day(s) away
    LATE_ARRIVAL = "late_arrival"  # start_date only meaningful; expected_arrival_time set


class AbsenceRequest(SQLModel, table=True):
    """A parent notifying the school in advance that their child will be
    absent, OR arriving late — distinct from Attendance, which staff mark
    after the fact, and from GateAttendance (models/gate_attendance.py),
    which logs the actual gate timestamp when it happens. Approving one does
    not itself write an Attendance/GateAttendance row: those stay the
    teacher's/gate staff's own call, this just gives advance notice and a
    paper trail. Late-arrival requests reuse this same table/workflow rather
    than a parallel one — same parent-submits/staff-reviews shape, just a
    single day and an expected time instead of a date range."""
    __tablename__ = "absence_requests"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(sa_column=Column(String, ForeignKey("students.id", ondelete="CASCADE"), index=True))
    requested_by: str = Field(index=True)  # User.id of the parent
    request_type: str = AbsenceRequestType.ABSENCE.value
    start_date: str
    end_date: str
    expected_arrival_time: Optional[str] = None  # "HH:MM" — only meaningful when request_type == late_arrival
    reason: str
    status: str = RequestStatus.PENDING.value
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    review_notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AbsenceRequestCreate(SQLModel):
    student_id: str
    request_type: AbsenceRequestType = AbsenceRequestType.ABSENCE
    start_date: str
    end_date: str
    expected_arrival_time: Optional[str] = None
    reason: str


class AbsenceRequestReview(SQLModel):
    status: RequestStatus
    review_notes: Optional[str] = None


class DocumentType(str, Enum):
    TRANSFER_CERTIFICATE = "transfer_certificate"
    LEAVING_CERTIFICATE = "leaving_certificate"
    TRANSCRIPT = "transcript"
    BONAFIDE_LETTER = "bonafide_letter"
    ID_CARD_REPLACEMENT = "id_card_replacement"
    OTHER = "other"


class DocumentRequestStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    READY = "ready"
    COMPLETED = "completed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class DocumentRequest(SQLModel, table=True):
    """A parent/student asking the school for an official document.
    Covers both certificate-backed types (transfer/leaving — fulfilled by
    linking the CertificateIssuance the existing /certificates/{id}/generate
    endpoint already produces) and free-form ones (transcripts, letters —
    fulfilled by uploading a file directly), since both are the same
    request/review/fulfil shape and don't warrant two separate tables."""
    __tablename__ = "document_requests"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(sa_column=Column(String, ForeignKey("students.id", ondelete="CASCADE"), index=True))
    requested_by: str = Field(index=True)
    document_type: str
    reason: Optional[str] = None
    status: str = DocumentRequestStatus.PENDING.value
    reviewed_by: Optional[str] = None
    review_notes: Optional[str] = None
    # Fulfilled either by linking an existing certificate issuance...
    certificate_issuance_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("certificate_issuances.id", ondelete="SET NULL"), index=True),
    )
    # ...or by uploading a file directly (transcripts, letters — nothing in
    # the certificate-template system covers these).
    fulfillment_file_url: Optional[str] = None
    completed_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class DocumentRequestCreate(SQLModel):
    student_id: str
    document_type: DocumentType
    reason: Optional[str] = None


class DocumentRequestReview(SQLModel):
    status: DocumentRequestStatus
    review_notes: Optional[str] = None


class DocumentRequestFulfill(SQLModel):
    certificate_issuance_id: Optional[str] = None
    fulfillment_file_url: Optional[str] = None
