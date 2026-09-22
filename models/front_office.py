"""Front Office / Visitor Management Models"""
from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime
from enum import Enum
import uuid


class VisitorStatus(str, Enum):
    CHECKED_IN = "checked_in"
    CHECKED_OUT = "checked_out"


class VisitorApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class FrontOfficeVisitor(SQLModel, table=True):
    """Record of a general (non-hostel) campus visitor"""
    __tablename__ = "front_office_visitors"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)

    visitor_name: str
    visitor_phone: Optional[str] = None
    purpose: str

    host_staff_id: Optional[str] = Field(default=None, index=True)
    person_to_see: Optional[str] = None

    badge_number: str = Field(index=True)
    id_type_shown: Optional[str] = None

    check_in_time: datetime = Field(default_factory=datetime.utcnow)
    check_out_time: Optional[datetime] = None
    status: VisitorStatus = VisitorStatus.CHECKED_IN

    # User.id of whoever checked the visitor in (server-set — not accepted
    # from FrontOfficeVisitorCreate). Needed for the maker-checker
    # segregation-of-duties check below.
    created_by: Optional[str] = None

    # Maker-checker (School.require_maker_checker, off by default): a
    # visitor is always checked in immediately regardless of this setting —
    # physical access can't be retroactively deferred. When enabled, the
    # check-in record starts PENDING and needs sign-off from a different
    # staff member via routers/front_office.py::approve_visitor — a pure
    # audit/QA confirmation, not a gate on access (mirrors clinic visits'
    # approach, not stock issuances'). When disabled (the default), records
    # are auto-approved at check-in — identical to this module's original
    # behavior.
    approval_status: VisitorApprovalStatus = VisitorApprovalStatus.APPROVED
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None

    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class FrontOfficeVisitorCreate(SQLModel):
    visitor_name: str
    visitor_phone: Optional[str] = None
    purpose: str
    host_staff_id: Optional[str] = None
    person_to_see: Optional[str] = None
    id_type_shown: Optional[str] = None
    notes: Optional[str] = None


class FrontOfficeVisitorUpdate(SQLModel):
    visitor_name: Optional[str] = None
    visitor_phone: Optional[str] = None
    purpose: Optional[str] = None
    host_staff_id: Optional[str] = None
    person_to_see: Optional[str] = None
    id_type_shown: Optional[str] = None
    notes: Optional[str] = None


class RejectVisitorRequest(SQLModel):
    rejection_reason: Optional[str] = None


class GatePass(SQLModel, table=True):
    """Outgoing gate pass for a student or staff member leaving campus
    during hours, with an expected/actual return time and optional
    maker-checker approval (School.require_maker_checker)."""
    __tablename__ = "gate_passes"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)

    person_type: str  # "student" | "staff"
    person_id: str = Field(index=True)
    reason: str

    expected_return_time: Optional[datetime] = None
    actual_return_time: Optional[datetime] = None

    approver_id: Optional[str] = Field(default=None, index=True)
    pass_number: str = Field(index=True)
    status: str = "pending"  # pending | approved | rejected | out | returned
    exit_time: Optional[datetime] = None

    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class GatePassCreate(SQLModel):
    person_type: str
    person_id: str
    reason: str
    expected_return_time: Optional[datetime] = None


class GatePassUpdate(SQLModel):
    reason: Optional[str] = None
    expected_return_time: Optional[datetime] = None
    actual_return_time: Optional[datetime] = None


class Appointment(SQLModel, table=True):
    """A scheduled visitor appointment with a staff member — bridges into
    the walk-in flow via POST /appointments/{id}/check-in, which creates a
    real FrontOfficeVisitor record when the visitor arrives."""
    __tablename__ = "front_office_appointments"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)

    staff_to_meet_id: str = Field(index=True)
    visitor_name: str
    visitor_phone: Optional[str] = None
    purpose: str
    requested_datetime: datetime

    status: str = "requested"  # requested | confirmed | cancelled | completed | no_show
    checked_in_visitor_id: Optional[str] = Field(default=None, index=True)

    notes: Optional[str] = None
    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AppointmentCreate(SQLModel):
    staff_to_meet_id: str
    visitor_name: str
    visitor_phone: Optional[str] = None
    purpose: str
    requested_datetime: datetime
    notes: Optional[str] = None


class AppointmentUpdate(SQLModel):
    staff_to_meet_id: Optional[str] = None
    visitor_name: Optional[str] = None
    visitor_phone: Optional[str] = None
    purpose: Optional[str] = None
    requested_datetime: Optional[datetime] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class CourierItem(SQLModel, table=True):
    """Inbound/outbound courier & mail tracking at the front desk."""
    __tablename__ = "front_office_courier_items"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)

    direction: str  # "inbound" | "outbound"
    sender: str
    recipient_staff_id: Optional[str] = Field(default=None, index=True)
    item_description: str
    tracking_number: Optional[str] = None
    courier_company: Optional[str] = None

    received_at: Optional[datetime] = None
    dispatched_at: Optional[datetime] = None
    status: str = "received"  # received | notified | collected | dispatched

    notes: Optional[str] = None
    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class CourierItemCreate(SQLModel):
    direction: str
    sender: str
    recipient_staff_id: Optional[str] = None
    item_description: str
    tracking_number: Optional[str] = None
    courier_company: Optional[str] = None
    notes: Optional[str] = None


class CourierItemUpdate(SQLModel):
    status: Optional[str] = None
    recipient_staff_id: Optional[str] = None
    item_description: Optional[str] = None
    tracking_number: Optional[str] = None
    courier_company: Optional[str] = None
    notes: Optional[str] = None
    received_at: Optional[datetime] = None
    dispatched_at: Optional[datetime] = None
