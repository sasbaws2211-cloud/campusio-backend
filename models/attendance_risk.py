"""Proactive attendance-risk alerting — distinct from routers/sms.py's
send_attendance_alert_sms, which is a manual tool a staff member triggers
by hand with a percentage they supply themselves. This is the automated
counterpart: a scheduled sweep (services/attendance_risk_service.py) computes
each active student's real attendance rate over a rolling window and fires
on its own once it crosses a school-configured threshold — same
settings+dedupe-log shape as models/fee_reminders.py.
"""
from datetime import datetime
from typing import Optional
import uuid

from sqlmodel import SQLModel, Field


class AttendanceRiskSettings(SQLModel, table=True):
    __tablename__ = "attendance_risk_settings"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True, unique=True)
    enabled: bool = True
    threshold_percent: float = Field(default=75.0)  # alert when rate drops below this
    lookback_days: int = Field(default=30)  # rolling window the rate is computed over
    cooldown_days: int = Field(default=7)  # don't re-alert for the same student sooner than this
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AttendanceRiskSettingsUpdate(SQLModel):
    enabled: Optional[bool] = None
    threshold_percent: Optional[float] = None
    lookback_days: Optional[int] = None
    cooldown_days: Optional[int] = None


class AttendanceRiskAlert(SQLModel, table=True):
    """Audit log of alerts actually sent — one row per student/channel per
    firing, so the cooldown check never double-sends within the window."""
    __tablename__ = "attendance_risk_alerts"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)
    attendance_rate: float
    channel: str  # "sms" or "email" or "in_app"
    message: str
    recipient: str
    sent: bool = False
    sent_at: Optional[datetime] = None
    status: str = "pending"  # pending, sent, failed
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
