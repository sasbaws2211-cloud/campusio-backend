"""Leave encashment models

leave_days is entered manually by HR/Admin but is validated against, and
deducted from, the staff member's ANNUAL LeaveBalance row (models/leave_request.py)
at request-creation and again at approval time — see
services/leave_encashment_service.py. daily_rate is derived from the staff's
active PayrollContract (basic_salary / 30) at request time.
"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Column
from sqlalchemy.types import Enum as SQLEnum
from typing import Optional
from datetime import datetime
import uuid
from enum import Enum


class LeaveEncashmentStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    PAID = "paid"


class LeaveEncashmentRequest(SQLModel, table=True):
    __tablename__ = "leave_encashment_requests"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    staff_id: str = Field(index=True)

    leave_days: float
    daily_rate: float
    encashment_amount: float
    reason: Optional[str] = None

    # Stored as plain varchar (native_enum=False) — same reasoning as
    # StaffLoan.status.
    status: LeaveEncashmentStatus = Field(
        default=LeaveEncashmentStatus.PENDING,
        sa_column=Column(
            SQLEnum('pending', 'approved', 'rejected', 'paid', name='leaveencashmentstatus', native_enum=False)
        ),
    )

    requested_by: Optional[str] = None
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None

    # Set once paid out through a payroll run.
    payroll_run_id: Optional[str] = None
    payroll_adjustment_id: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class LeaveEncashmentCreate(SQLModel):
    staff_id: str
    leave_days: float
    reason: Optional[str] = None
