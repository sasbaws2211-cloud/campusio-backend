"""Staff and Teacher models"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey
from sqlalchemy.types import Enum as SQLEnum
from typing import Optional
from datetime import datetime
from enum import Enum
import uuid
from models.user import UserRole


class StaffType(str, Enum):
    TEACHING = "teaching"
    NON_TEACHING = "non_teaching"
    ADMIN = "admin"


class StaffStatus(str, Enum):
    ACTIVE = "active"
    ON_LEAVE = "on_leave"
    RESIGNED = "resigned"
    TERMINATED = "terminated"


class PayoutVerificationStatus(str, Enum):
    UNSUBMITTED = "unsubmitted"
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"


class Staff(SQLModel, table=True):
    __tablename__ = "staff"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    staff_id: str = Field(index=True)
    first_name: str
    last_name: str
    other_names: Optional[str] = None
    email: str
    phone: str
    date_of_birth: str
    gender: str
    staff_type: StaffType
    position: str
    department: Optional[str] = None
    qualification: Optional[str] = None
    date_joined: str
    address: Optional[str] = None
    photo_url: Optional[str] = None
    status: StaffStatus = StaffStatus.ACTIVE
    role: Optional[UserRole] = Field(
        default=None,
        sa_column=Column(SQLEnum(UserRole, name='staffrole', native_enum=False), nullable=True),
    )
    user_id: Optional[str] = Field(default=None, index=True)

    # Which Shift governs this staff member's late-cutoff on self clock-in —
    # no DB FK (consistent with every other cross-reference on this model),
    # no history kept if reassigned. None falls back to the school's default
    # shift, or if none is configured either, self clock-in is always PRESENT
    # (today's exact behavior, preserved so schools that never touch shifts
    # see no change).
    shift_id: Optional[str] = Field(default=None, index=True)

    # The numeric PIN this staff member was assigned on a school's
    # fingerprint/face terminal at local enrollment time — see
    # routers/biometric_adms.py. Mirrors Student.biometric_device_pin
    # (models/student.py): not a secret, just the join key between a raw
    # ADMS device push and this staff record. NULL means not enrolled on
    # any terminal.
    biometric_device_pin: Optional[str] = Field(default=None, index=True)

    campus_id: Optional[str] = Field(default=None, index=True)

    # Reporting line — who this staff member's manager is, for performance
    # appraisal visibility (a manager sees their direct reports' reviews)
    # and any future manager-scoped views. No DB FK, consistent with the
    # rest of this model's cross-references; self-referential (points at
    # another staff.id) so left null rather than enforced not-null since
    # not every school will populate a full org chart on day one.
    manager_id: Optional[str] = Field(default=None, index=True)

    # Payout settlement details — where money for staff-initiated paid
    # activities (currently: extra classes) is sent via a Paystack
    # subaccount. Staff submits these themselves; an admin verifies once
    # before the subaccount is created and starts receiving real money.
    # Reusable beyond extra classes (e.g. payroll) since it lives on Staff,
    # not on any one module's records.
    payout_account_type: Optional[str] = None  # "bank" or "mobile_money"
    payout_bank_code: Optional[str] = None
    payout_account_number: Optional[str] = None
    payout_account_name: Optional[str] = None  # confirmed via Paystack's resolve-account-number check
    paystack_subaccount_code: Optional[str] = None
    # Stored as plain varchar (native_enum=False), not a native Postgres
    # enum — sidesteps the name/value casing mismatch that a bare `str,
    # Enum` column hits on this codebase's Postgres setup (ALTER TYPE would
    # be needed for every new member, and it must match the member *name*,
    # not its value).
    payout_verification_status: PayoutVerificationStatus = Field(
        default=PayoutVerificationStatus.UNSUBMITTED,
        sa_column=Column(SQLEnum('unsubmitted', 'pending', 'verified', 'rejected', name='payoutverificationstatus', native_enum=False)),
    )
    payout_submitted_at: Optional[datetime] = None
    payout_verified_at: Optional[datetime] = None
    payout_verified_by: Optional[str] = None  # User.id of the admin who verified/rejected it
    payout_rejection_reason: Optional[str] = None

    # Probation — plain varchar (native_enum=False elsewhere in this file's
    # own convention), not a real state machine: "not_applicable" is the
    # default for every existing staff row so nothing changes for schools
    # that don't use this. Values: not_applicable/on_probation/confirmed/
    # extended/terminated. See routers/hr_admin.py's probation endpoints.
    probation_status: str = Field(default="not_applicable")
    probation_end_date: Optional[str] = None
    probation_confirmed_at: Optional[datetime] = None
    probation_confirmed_by: Optional[str] = None  # User.id

    # Statutory registration — used on the SSNIT contribution export
    # (services/statutory_export_service.py). Optional: not every staff
    # member is necessarily SSNIT-registered when first added.
    ssnit_number: Optional[str] = None

    # Contract-term classification — independent of PayrollContract (which
    # only exists for staff actually on this school's payroll; an NSS
    # posting is typically paid a government allowance and may never get a
    # PayrollContract row, so it needs its own expiry tracking here rather
    # than relying on payroll's contract-expiry reminder). Values:
    # permanent/fixed_term/nss/contract. Separate from probation_status
    # above, which tracks whether a PERMANENT hire is still proving
    # themselves, not the term structure itself.
    employment_type: str = Field(default="permanent")
    contract_end_date: Optional[str] = None

    # CPD/professional-development requirement — None means this staff
    # member has no tracked requirement (every existing row's default,
    # so nothing changes for a school that doesn't use this). When set,
    # GET /hr/development/training/cpd-compliance compares it against the
    # calendar year's summed StaffTraining.hours for them.
    annual_cpd_hours_required: Optional[float] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class StaffCreate(SQLModel):
    staff_id: Optional[str] = None
    first_name: str
    last_name: str
    other_names: Optional[str] = None
    email: str
    phone: str
    date_of_birth: str
    gender: str
    staff_type: StaffType
    position: str
    department: Optional[str] = None
    qualification: Optional[str] = None
    date_joined: str
    address: Optional[str] = None
    photo_url: Optional[str] = None
    campus_id: Optional[str] = None
    role: Optional[UserRole] = None
    manager_id: Optional[str] = None
    employment_type: str = "permanent"
    contract_end_date: Optional[str] = None
    annual_cpd_hours_required: Optional[float] = None


class StaffShiftHistory(SQLModel, table=True):
    """Audit trail for Staff.shift_id changes — models/shift.py's own
    module docstring used to say plainly "no shift-change history is
    kept." Purely additive: nothing reads this except the new history
    endpoint, so a reassignment's own behavior (takes effect immediately)
    is unchanged."""
    __tablename__ = "staff_shift_history"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    staff_id: str = Field(index=True)
    old_shift_id: Optional[str] = None
    new_shift_id: Optional[str] = None
    changed_by: str
    changed_at: datetime = Field(default_factory=datetime.utcnow)


class TeacherAssignment(SQLModel, table=True):
    __tablename__ = "teacher_assignments"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    staff_id: str = Field(index=True)
    class_id: str = Field(index=True)
    subject_id: str = Field(index=True)
    academic_term_id: str = Field(sa_column=Column(String, ForeignKey("academic_terms.id", ondelete="CASCADE"), index=True))
    is_class_teacher: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)


class TeacherAssignmentUpdate(SQLModel):
    staff_id: Optional[str] = None
    is_class_teacher: Optional[bool] = None
