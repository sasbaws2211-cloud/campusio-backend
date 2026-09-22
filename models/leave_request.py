"""Staff leave-request models — request/approval workflow with a balance
ledger (LeaveBalance below). Approving a request here creates real
StaffAttendance(status=EXCUSED) rows. LeaveEncashmentRequest
(models/leave_encashment.py) draws against this same ledger's ANNUAL row
rather than keeping its own separate balance.
"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Column
from sqlalchemy.types import Enum as SQLEnum
from typing import Optional, List
from datetime import datetime
from enum import Enum
import uuid


class LeaveType(str, Enum):
    ANNUAL = "annual"
    SICK = "sick"
    CASUAL = "casual"
    UNPAID = "unpaid"  # exempt from balance tracking — unlimited by definition; reduces pay, see services/payroll_service.py::get_unpaid_leave_days
    # Both exempt from balance tracking, same as UNPAID (a school doesn't
    # have to pre-guess an entitlement figure) — but unlike UNPAID, neither
    # is read by get_unpaid_leave_days, so approved MATERNITY/STUDY leave
    # is paid in full, matching Ghana's statutory paid-maternity-leave
    # expectation. Previously a pregnant teacher's leave had no distinct
    # type at all and had to be filed as generic sick or unpaid leave.
    MATERNITY = "maternity"
    STUDY = "study"


class LeaveRequestStatus(str, Enum):
    PENDING = "pending"
    # Inserted only when the requester has a Staff.manager_id set — a
    # request with no manager assigned skips straight from PENDING to
    # APPROVED/REJECTED by HR/admin, exactly today's behavior, so schools
    # that never populate manager_id see no change at all.
    MANAGER_APPROVED = "manager_approved"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"  # staff withdrew their own request pre-approval
    REVOKED = "revoked"      # admin reversed an already-approved request


class LeaveBalance(SQLModel, table=True):
    """One row per staff member, leave type, and year. UNPAID leave never
    gets a row — see LeaveType.UNPAID."""
    __tablename__ = "leave_balances"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    staff_id: str = Field(index=True)
    leave_type: LeaveType = Field(
        sa_column=Column(SQLEnum('annual', 'sick', 'casual', 'unpaid', 'maternity', 'study', name='leavetype_balance', native_enum=False))
    )
    year: int = Field(index=True)
    entitlement_days: float
    used_days: float = Field(default=0.0)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class LeaveRequest(SQLModel, table=True):
    __tablename__ = "leave_requests"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    staff_id: str = Field(index=True)
    leave_type: LeaveType = Field(
        sa_column=Column(SQLEnum('annual', 'sick', 'casual', 'unpaid', 'maternity', 'study', name='leavetype_request', native_enum=False))
    )
    start_date: str  # "YYYY-MM-DD", matches StaffAttendance.attendance_date
    end_date: str
    days_requested: float  # server-computed working-day count, never trusted from the client
    reason: Optional[str] = None

    status: LeaveRequestStatus = Field(
        default=LeaveRequestStatus.PENDING,
        sa_column=Column(
            SQLEnum('pending', 'manager_approved', 'approved', 'rejected', 'cancelled', 'revoked', name='leaverequeststatus', native_enum=False)
        ),
    )

    requested_by: Optional[str] = None
    manager_approved_by: Optional[str] = None
    manager_approved_at: Optional[datetime] = None
    manager_rejection_reason: Optional[str] = None
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    revoked_by: Optional[str] = None
    revoked_at: Optional[datetime] = None
    revoke_reason: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class LeaveRequestCreate(SQLModel):
    staff_id: Optional[str] = None  # admin-only override; self-service ignores this and uses the caller's own Staff profile
    leave_type: LeaveType
    start_date: str
    end_date: str
    reason: Optional[str] = None


class LeaveRequestReject(SQLModel):
    rejection_reason: Optional[str] = None


class LeaveRequestRevoke(SQLModel):
    revoke_reason: Optional[str] = None


class LeaveBalanceSeedRequest(SQLModel):
    year: int
    staff_ids: Optional[List[str]] = None  # None = seed every active staff member in the school


class LeaveBalanceUpdate(SQLModel):
    entitlement_days: float
