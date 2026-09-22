"""Gate/entrance attendance: a security-desk arrival/departure log, distinct
from both the teacher-marked classroom Attendance (models/attendance.py —
present/absent/late by roll call, no timestamp) and the QR pickup-dispatch
system (models/security.py — who's collecting a child and whether they've
arrived at the gate to do so). This is the third, simplest piece: "what time
did this student physically pass through the gate today, in and out."

Plain str columns throughout (dates as 'YYYY-MM-DD', times as 'HH:MM') —
same enum/native-type avoidance convention as every other model added this
project (see models/parent_requests.py's module docstring for the reasoning).
"""
from datetime import datetime
from typing import Optional
import uuid

from sqlmodel import SQLModel, Field


class GateAttendanceSettings(SQLModel, table=True):
    """One row per school. Both cutoffs are optional — leaving either unset
    disables lateness detection for that side (check-in or pickup) without
    disabling gate logging itself, so a school can start logging arrivals
    before deciding on a cutoff policy."""
    __tablename__ = "gate_attendance_settings"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True, unique=True)
    late_cutoff_time: Optional[str] = None          # "HH:MM" 24h — after this, a check-in is flagged late
    pickup_late_cutoff_time: Optional[str] = None    # "HH:MM" 24h — after this, a check-out is flagged a late pickup
    # Opt-in: when true, a gate check-in also auto-creates today's classroom
    # models.attendance.Attendance row as PRESENT if none exists yet — see
    # services/gate_attendance_service.record_check_in. Off by default since
    # writing into a second system is a real behavior change a school should
    # choose, not inherit silently.
    auto_mark_classroom_attendance: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class GateAttendance(SQLModel, table=True):
    """One row per student per calendar day. check_in_* fields are set by
    routers/gate_attendance.py's check-in endpoint (gate/security staff);
    check_out_* fields are set either by that same router's check-out
    endpoint OR by routers/security.py's pickup-completion flow when a
    school uses the QR pickup system instead of manual gate checkout —
    check_out_method distinguishes which."""
    __tablename__ = "gate_attendance"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)
    date: str = Field(index=True)  # "YYYY-MM-DD"

    check_in_time: Optional[datetime] = None
    check_in_by: Optional[str] = None  # User.id of the staff member who logged it
    check_in_method: Optional[str] = None  # "gate" | "qr_id_card" | "biometric"
    is_late: bool = False

    check_out_time: Optional[datetime] = None
    check_out_by: Optional[str] = None
    check_out_method: Optional[str] = None  # "gate" | "qr_pickup" | "qr_id_card" | "biometric"
    is_late_pickup: bool = False

    notes: Optional[str] = None
    # Set by services/gate_attendance_service.run_gate_pickup_escalation_sweep
    # once a "checked in, never checked out" alert has fired for this row —
    # keeps the daily sweep from re-alerting the same student every time it
    # runs (e.g. after a misfire retry the same day).
    escalated_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class GateCheckInRequest(SQLModel):
    student_id: str
    notes: Optional[str] = None


class GateCheckOutRequest(SQLModel):
    student_id: str
    notes: Optional[str] = None


class GateScanRequest(SQLModel):
    """A staff member scanning a student's ID card at the gate — card_number
    is IDCard.qr_payload/card_number (models/certificates.py), the same
    static value already printed as a QR on the card."""
    card_number: str
    notes: Optional[str] = None


class UpdateGateAttendanceSettingsRequest(SQLModel):
    late_cutoff_time: Optional[str] = None
    pickup_late_cutoff_time: Optional[str] = None
    auto_mark_classroom_attendance: Optional[bool] = None
