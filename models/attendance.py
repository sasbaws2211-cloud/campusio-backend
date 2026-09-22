"""Attendance models"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey, UniqueConstraint
from typing import Optional
from datetime import datetime
from enum import Enum
import uuid


class AttendanceStatus(str, Enum):
    PRESENT = "present"
    ABSENT = "absent"
    LATE = "late"
    EXCUSED = "excused"


class Attendance(SQLModel, table=True):
    __tablename__ = "attendance"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)
    class_id: str = Field(index=True)
    academic_term_id: str = Field(sa_column=Column(String, ForeignKey("academic_terms.id", ondelete="CASCADE"), index=True))
    attendance_date: str = Field(index=True)
    status: AttendanceStatus
    remarks: Optional[str] = None
    recorded_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AttendanceCreate(SQLModel):
    student_id: str
    class_id: str
    academic_term_id: str
    attendance_date: str
    status: AttendanceStatus
    remarks: Optional[str] = None


class AttendanceBulkCreate(SQLModel):
    class_id: str
    academic_term_id: str
    attendance_date: str
    records: list[dict]


class StaffAttendance(SQLModel, table=True):
    __tablename__ = "staff_attendance"
    __table_args__ = (UniqueConstraint("staff_id", "attendance_date", name="uq_staff_attendance_staff_date"),)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    staff_id: str = Field(index=True)
    attendance_date: str = Field(index=True)
    check_in: Optional[str] = None
    check_out: Optional[str] = None
    status: AttendanceStatus
    remarks: Optional[str] = None
    recorded_by: Optional[str] = None

    # Self-service clock-in/out soft signals — logged for HR review, never
    # used to block a clock-in outright. ip_flagged is only meaningful when
    # the school has configured StaffAttendanceSettings.allowed_ip_cidr.
    clock_in_ip: Optional[str] = None
    clock_out_ip: Optional[str] = None
    ip_flagged: bool = Field(default=False)
    via_qr: bool = Field(default=False)

    # Set when this row was auto-created by an approved LeaveRequest (status
    # will be EXCUSED) — lets revoke_request find and remove exactly the rows
    # it created without touching manually-recorded ones.
    leave_request_id: Optional[str] = Field(default=None, index=True)

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class StaffAttendanceCreate(SQLModel):
    staff_id: str
    attendance_date: str
    status: AttendanceStatus
    check_in: Optional[str] = None
    check_out: Optional[str] = None
    remarks: Optional[str] = None


class StaffAttendanceBulkCreate(SQLModel):
    attendance_date: str
    records: list[dict]  # each: {staff_id, status, check_in?, check_out?, remarks?}


class ClockInMode(str, Enum):
    OPEN = "open"  # self-service button works from anywhere; IP still logged as a soft signal
    QR_REQUIRED = "qr_required"  # must scan the gate QR code to clock in/out


class StaffAttendanceSettings(SQLModel, table=True):
    """Per-school configuration for self-service clock-in/out — lets each
    school choose how strict they want to be, rather than baking one policy
    into the app for everyone."""
    __tablename__ = "staff_attendance_settings"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True, unique=True)
    clock_in_mode: ClockInMode = Field(default=ClockInMode.OPEN)

    # Soft signal only — never blocks a clock-in, just flags it for review.
    allowed_ip_cidr: Optional[str] = None  # e.g. "41.203.64.0/24"; comma-separated for multiple ranges

    # QR-gate mode: the current active token + when it was issued. Checked
    # against qr_token_ttl_minutes at verification time rather than needing
    # a scheduler to expire it — same lazy self-healing pattern used
    # elsewhere in this codebase.
    current_qr_token: Optional[str] = None
    qr_token_generated_at: Optional[datetime] = None
    qr_token_ttl_minutes: int = Field(default=480)  # default: valid for one school day

    updated_at: datetime = Field(default_factory=datetime.utcnow)
    updated_by: Optional[str] = None


class StaffAttendanceSettingsUpdate(SQLModel):
    clock_in_mode: Optional[ClockInMode] = None
    allowed_ip_cidr: Optional[str] = None
    qr_token_ttl_minutes: Optional[int] = None


class ClockRequest(SQLModel):
    qr_token: Optional[str] = None
