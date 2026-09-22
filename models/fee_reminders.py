"""Automated fee-due reminders to parents — the student-fee counterpart to
services/payment_reminder_service.py, which only reminds SCHOOLS about
their own Campusio platform-subscription billing. That job's shape
(per-entity config, a dedupe-by-day-count audit log, SMS+email) is mirrored
here deliberately for the parent-fee case."""
from datetime import datetime
from typing import Optional
import uuid

from sqlmodel import SQLModel, Field


class FeeReminderSettings(SQLModel, table=True):
    __tablename__ = "fee_reminder_settings"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True, unique=True)
    enabled: bool = True
    reminder_days_before_due: int = Field(default=7)  # start reminding this many days out; always reminds once overdue
    # Late-fee automation — off by default so no school gets surprise
    # charges until an admin opts in via PUT /fee-reminders/settings.
    enable_late_fees: bool = False
    late_fee_percentage: float = Field(default=0)  # % of the outstanding balance charged once
    late_fee_grace_days: int = Field(default=0)  # days past due date before the penalty applies
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class FeeReminderSettingsUpdate(SQLModel):
    enabled: Optional[bool] = None
    reminder_days_before_due: Optional[int] = None
    enable_late_fees: Optional[bool] = None
    late_fee_percentage: Optional[float] = None
    late_fee_grace_days: Optional[int] = None


class FeeReminder(SQLModel, table=True):
    """Audit log of reminders actually sent — one row per fee/day-count/
    channel, so send_pending_reminders never double-sends the same day's
    reminder if run twice."""
    __tablename__ = "fee_reminders"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    fee_id: str = Field(index=True)
    student_id: str = Field(index=True)
    channel: str  # "sms" or "email"
    days_until_due: int
    message: str
    recipient: str
    sent: bool = False
    sent_at: Optional[datetime] = None
    status: str = "pending"  # pending, sent, failed
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
