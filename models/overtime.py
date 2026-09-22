"""Staff overtime tracking. Distinct from PayrollAdjustment (models/payroll.py) —
an OvertimeRecord is the claim/approval record; once approved it can be
turned into a PayrollAdjustment (see routers/hr_overtime.py's
push-to-payroll action) so the actual payment still goes through payroll's
existing math rather than this module inventing its own.
"""
from datetime import datetime
from typing import Optional
import uuid

from sqlmodel import Field, SQLModel
from sqlalchemy import Column, String, ForeignKey


class OvertimeRecord(SQLModel, table=True):
    __tablename__ = "hr_overtime_records"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    staff_id: str = Field(sa_column=Column(String, ForeignKey("staff.id", ondelete="CASCADE"), index=True))
    work_date: str  # "YYYY-MM-DD"
    hours: float
    rate_multiplier: float = 1.5  # e.g. 1.5x normal hourly rate
    reason: Optional[str] = None
    status: str = Field(default="pending", index=True)  # pending/approved/rejected/paid
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    payroll_adjustment_id: Optional[str] = None  # set once pushed into a PayrollAdjustment
    recorded_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class OvertimeRecordCreate(SQLModel):
    staff_id: str
    work_date: str
    hours: float
    rate_multiplier: float = 1.5
    reason: Optional[str] = None


class OvertimeRecordReject(SQLModel):
    rejection_reason: Optional[str] = None
