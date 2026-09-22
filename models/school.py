"""School and Academic Term models"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Column
from sqlalchemy.types import Enum as SQLEnum
from typing import Optional
from datetime import datetime
from enum import Enum
import uuid

from models.staff import PayoutVerificationStatus


class SchoolType(str, Enum):
    BASIC = "basic"
    JHS = "jhs"
    COMBINED = "combined"


class School(SQLModel, table=True):
    __tablename__ = "schools"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    name: str = Field(index=True)
    code: str = Field(unique=True, index=True)
    school_type: SchoolType
    address: str
    city: str
    region: str
    phone: str
    email: str
    logo_url: Optional[str] = None
    motto: Optional[str] = None
    is_active: bool = True
    # Feature toggles — boarding is an SHS phenomenon, so the hostel module is
    # OFF by default for new Basic/JHS schools and enabled per school when needed.
    enable_hostel: bool = False
    # Billing enforcement — a super admin's manual "cut off access" switch,
    # deliberately separate from is_active (which means "school removed").
    # Enforced centrally in auth.py::get_current_user for every non-super-admin
    # request against this school.
    access_suspended: bool = False
    access_suspended_reason: Optional[str] = None
    access_suspended_at: Optional[datetime] = None
    # Emergency pickup lockdown — a school-wide "suspend every student
    # release" switch for a crisis (security threat, severe weather, a
    # medical emergency on campus), distinct from the per-student
    # StudentSecurityProfile.release_hold this mirrors in shape. Checked
    # everywhere a student can be released (QR pickup scan, staff status
    # change, parent self-confirm, "on my way", transport dispatch) via
    # routers/security.py's assert_release_allowed.
    pickup_lockdown_active: bool = False
    pickup_lockdown_reason: Optional[str] = None
    pickup_lockdown_set_by: Optional[str] = None
    pickup_lockdown_set_at: Optional[datetime] = None
    # Finance settings
    base_currency: str = Field(default="GHS")  # GL accounts and reports are all in this currency
    # Segregation of duties: when True, the person who submitted an expense or
    # created a journal entry can never be the one who approves/posts it.
    # Off by default — small schools with a single finance staffer can't
    # otherwise use approval workflows at all.
    require_maker_checker: bool = False
    # Off by default — a school that hasn't budgeted every account yet, or
    # doesn't want spending hard-blocked, still gets the budget_warning
    # surfaced at expense-approval time regardless of this flag; turning it
    # on makes exceeding a budgeted account's remaining amount a real
    # rejection instead of only ever being a report to check afterward.
    enforce_budget_limits: bool = False
    # Off by default — when a student is admitted after a term has already
    # started, the fee charged for that term is otherwise the same full
    # amount as a student enrolled since day one. Turning this on prorates
    # a new fee's amount_due by the fraction of the term remaining as of
    # the student's admission_date (see routers/fees.py::_prorate_fee_amount).
    prorate_fees_for_late_admission: bool = False
    # Off by default — previously the only fee-balance enforcement anywhere
    # was at student exit/withdrawal (_compute_exit_clearance in
    # routers/students.py); a defaulting family otherwise saw zero
    # consequence until the student eventually left the school. Turning
    # this on blocks report-card APPROVAL (not generation — teachers can
    # still draft/edit) while the student has a fee balance outstanding,
    # overridable per-card via approve_report_card's override_fee_hold flag.
    hold_report_cards_for_fee_defaulters: bool = False
    # Statutory registration — used on the SSNIT contribution export
    # (services/statutory_export_service.py). Optional: schools that don't
    # export SSNIT schedules can leave this unset.
    ssnit_employer_number: Optional[str] = None
    # Public admissions — when enabled, the public apply form
    # (routers/public_admissions.py) requires payment of this amount before
    # an application counts as submitted. Off by default so schools that
    # don't charge an application fee see no change in the apply flow.
    require_application_fee: bool = False
    application_fee_amount: Optional[float] = None
    # Direct-settlement payout — where parent fee payments are sent via a
    # Paystack subaccount instead of the platform's pooled main balance. A
    # school submits its own bank/MoMo details; a super admin verifies once
    # before the subaccount is created and starts receiving real money.
    # Once set, payments split straight to the school's own bank/MoMo
    # account on Paystack's normal settlement schedule — no /transfer call,
    # so no Paystack dashboard approval step. Mirrors Staff's
    # identically-named fields (models/staff.py).
    payout_account_type: Optional[str] = None  # "bank" or "mobile_money"
    payout_bank_code: Optional[str] = None
    payout_account_number: Optional[str] = None
    payout_account_name: Optional[str] = None  # confirmed via Paystack's resolve-account-number check
    paystack_subaccount_code: Optional[str] = None
    payout_verification_status: PayoutVerificationStatus = Field(
        default=PayoutVerificationStatus.UNSUBMITTED,
        sa_column=Column(SQLEnum('unsubmitted', 'pending', 'verified', 'rejected', name='schoolpayoutverificationstatus', native_enum=False)),
    )
    payout_submitted_at: Optional[datetime] = None
    payout_verified_at: Optional[datetime] = None
    payout_verified_by: Optional[str] = None  # User.id of the super admin who verified/rejected it
    payout_rejection_reason: Optional[str] = None

    # A second, independent direct-settlement subaccount — for canteen
    # top-ups specifically. Deliberately separate from the fee payout above:
    # a school's canteen is often run against its own bank/MoMo account
    # (e.g. a canteen committee's account) distinct from the account that
    # collects tuition. Same submit-once/verify-once mechanics as the fee
    # payout fields; canteen top-ups fall back to the pooled main balance
    # until this is verified, same as fee payments do for the other one.
    canteen_payout_account_type: Optional[str] = None  # "bank" or "mobile_money"
    canteen_payout_bank_code: Optional[str] = None
    canteen_payout_account_number: Optional[str] = None
    canteen_payout_account_name: Optional[str] = None
    canteen_paystack_subaccount_code: Optional[str] = None
    canteen_payout_verification_status: PayoutVerificationStatus = Field(
        default=PayoutVerificationStatus.UNSUBMITTED,
        sa_column=Column(SQLEnum('unsubmitted', 'pending', 'verified', 'rejected', name='schoolcanteenpayoutverificationstatus', native_enum=False)),
    )
    canteen_payout_submitted_at: Optional[datetime] = None
    canteen_payout_verified_at: Optional[datetime] = None
    canteen_payout_verified_by: Optional[str] = None
    canteen_payout_rejection_reason: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class SchoolCreate(SQLModel):
    name: str
    code: str
    school_type: SchoolType
    address: str
    city: str
    region: str
    phone: str
    email: str
    logo_url: Optional[str] = None
    motto: Optional[str] = None


class SchoolUpdate(SQLModel):
    name: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    region: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    logo_url: Optional[str] = None
    motto: Optional[str] = None
    is_active: Optional[bool] = None
    enable_hostel: Optional[bool] = None
    base_currency: Optional[str] = None
    require_maker_checker: Optional[bool] = None
    ssnit_employer_number: Optional[str] = None
    require_application_fee: Optional[bool] = None
    application_fee_amount: Optional[float] = None


class TermType(str, Enum):
    FIRST = "first"
    SECOND = "second"
    THIRD = "third"


class AcademicTerm(SQLModel, table=True):
    __tablename__ = "academic_terms"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    academic_year_id: Optional[str] = Field(default=None, index=True)
    academic_year: str
    term: TermType
    start_date: str
    end_date: str
    is_current: bool = False
    # Once locked, the term's dates/is_current can't be changed and it can't be
    # deleted — and grades/attendance/assignments/fees can't be written against
    # it. Locking is a dedicated action (see /terms/{id}/lock in routers/schools.py),
    # not a generic field on AcademicTermUpdate, so it stays auditable.
    is_locked: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AcademicTermCreate(SQLModel):
    academic_year_id: Optional[str] = None
    academic_year: str
    term: TermType
    start_date: str
    end_date: str
    is_current: bool = False


class AcademicTermUpdate(SQLModel):
    academic_year_id: Optional[str] = None
    academic_year: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    is_current: Optional[bool] = None


class AcademicYearStatus(str, Enum):
    PLANNED = "planned"
    ACTIVE = "active"
    CLOSED = "closed"


class AcademicYear(SQLModel, table=True):
    __tablename__ = "academic_years"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    name: str
    start_date: str
    end_date: str
    status: AcademicYearStatus = AcademicYearStatus.PLANNED
    is_current: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AcademicYearCreate(SQLModel):
    name: str
    start_date: str
    end_date: str
    status: AcademicYearStatus = AcademicYearStatus.PLANNED
    is_current: bool = False


class AcademicYearUpdate(SQLModel):
    name: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    status: Optional[AcademicYearStatus] = None
    is_current: Optional[bool] = None


class CalendarEvent(SQLModel, table=True):
    __tablename__ = "calendar_events"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    academic_year_id: Optional[str] = Field(default=None, index=True)
    title: str
    event_type: str = "event"
    start_date: str
    end_date: str
    description: Optional[str] = None
    is_instructional: bool = False
    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class CalendarEventCreate(SQLModel):
    academic_year_id: Optional[str] = None
    title: str
    event_type: str = "event"
    start_date: str
    end_date: str
    description: Optional[str] = None
    is_instructional: bool = False


class CalendarEventUpdate(SQLModel):
    academic_year_id: Optional[str] = None
    title: Optional[str] = None
    event_type: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    description: Optional[str] = None
    is_instructional: Optional[bool] = None


class CalendarFeedToken(SQLModel, table=True):
    """A per-school secret authenticating the public, unauthenticated .ics
    subscription feed (Google Calendar / Outlook 'subscribe from URL' flows
    poll on their own schedule with no session or JWT available) — the
    token embedded in the feed URL path is the only auth. Rotating it
    invalidates every URL already handed out."""
    __tablename__ = "calendar_feed_tokens"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True, unique=True)
    token: str = Field(index=True, unique=True)
    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    rotated_at: Optional[datetime] = None
