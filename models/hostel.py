"""Hostel Management Models"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey, UniqueConstraint
from typing import Optional, List
from datetime import datetime
from enum import Enum
import uuid


class HostelStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    FULL = "full"
    MAINTENANCE = "maintenance"


class RoomType(str, Enum):
    SINGLE = "single"
    DOUBLE = "double"
    TRIPLE = "triple"
    DORMITORY = "dormitory"


class RoomStatus(str, Enum):
    VACANT = "vacant"
    OCCUPIED = "occupied"
    MAINTENANCE = "maintenance"
    RESERVED = "reserved"


class StudentHostelStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    GRADUATED = "graduated"
    TRANSFERRED = "transferred"


class HostelFeeType(str, Enum):
    MONTHLY = "monthly"
    TERM = "term"
    ANNUAL = "annual"
    SEMESTER = "semester"


class CheckInStatus(str, Enum):
    CHECKED_IN = "checked_in"
    CHECKED_OUT = "checked_out"
    ON_LEAVE = "on_leave"


class Hostel(SQLModel, table=True):
    """Hostel/Dormitory model"""
    __tablename__ = "hostels"
    __table_args__ = (UniqueConstraint("school_id", "hostel_code", name="uq_hostels_school_hostel_code"),)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    hostel_name: str = Field(index=True)
    # Hostel codes are a school's own internal labeling scheme, not a
    # globally unique identifier -- see uq_hostels_school_hostel_code above.
    hostel_code: str = Field(index=True)
    hostel_type: str  # Boys, Girls, Mixed
    capacity: int
    current_occupancy: int = 0
    
    # Contact and location
    warden_name: Optional[str] = None
    warden_phone: Optional[str] = None
    warden_email: Optional[str] = None
    location: Optional[str] = None
    address: Optional[str] = None
    
    # Facilities
    has_wifi: bool = False
    has_laundry: bool = False
    has_kitchen: bool = False
    has_common_room: bool = False
    has_security: bool = False
    
    status: HostelStatus = HostelStatus.ACTIVE
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class HostelCreate(SQLModel):
    hostel_name: str
    hostel_code: str
    hostel_type: str
    capacity: int
    warden_name: Optional[str] = None
    warden_phone: Optional[str] = None
    warden_email: Optional[str] = None
    location: Optional[str] = None
    address: Optional[str] = None
    has_wifi: bool = False
    has_laundry: bool = False
    has_kitchen: bool = False
    has_common_room: bool = False
    has_security: bool = False
    notes: Optional[str] = None


class HostelUpdate(SQLModel):
    hostel_name: Optional[str] = None
    hostel_code: Optional[str] = None
    hostel_type: Optional[str] = None
    capacity: Optional[int] = None
    warden_name: Optional[str] = None
    warden_phone: Optional[str] = None
    warden_email: Optional[str] = None
    location: Optional[str] = None
    address: Optional[str] = None
    has_wifi: Optional[bool] = None
    has_laundry: Optional[bool] = None
    has_kitchen: Optional[bool] = None
    has_common_room: Optional[bool] = None
    has_security: Optional[bool] = None
    status: Optional[HostelStatus] = None
    notes: Optional[str] = None


class Room(SQLModel, table=True):
    """Room model in a hostel"""
    __tablename__ = "rooms"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    hostel_id: str = Field(sa_column=Column(String, ForeignKey("hostels.id", ondelete="CASCADE"), index=True))
    room_number: str = Field(index=True)
    room_type: RoomType
    capacity: int
    current_occupancy: int = 0
    
    # Room details
    floor: int = 1
    has_bathroom: bool = True
    has_ac: bool = False
    has_heater: bool = False
    has_desk: bool = True
    has_bed: bool = True
    
    status: RoomStatus = RoomStatus.VACANT
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class RoomCreate(SQLModel):
    hostel_id: str
    room_number: str
    room_type: RoomType
    capacity: int
    floor: int = 1
    has_bathroom: bool = True
    has_ac: bool = False
    has_heater: bool = False
    has_desk: bool = True
    has_bed: bool = True
    notes: Optional[str] = None


class RoomUpdate(SQLModel):
    room_number: Optional[str] = None
    room_type: Optional[RoomType] = None
    capacity: Optional[int] = None
    floor: Optional[int] = None
    has_bathroom: Optional[bool] = None
    has_ac: Optional[bool] = None
    has_heater: Optional[bool] = None
    has_desk: Optional[bool] = None
    has_bed: Optional[bool] = None
    status: Optional[RoomStatus] = None
    notes: Optional[str] = None


class StudentHostel(SQLModel, table=True):
    """Student Hostel Accommodation"""
    __tablename__ = "student_hostels"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(unique=True, index=True)
    hostel_id: str = Field(sa_column=Column(String, ForeignKey("hostels.id", ondelete="CASCADE"), index=True))
    room_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("rooms.id", ondelete="SET NULL"), index=True)
    )

    # Enrollment details
    check_in_date: str
    check_out_date: Optional[str] = None
    academic_year: str
    
    # Contact information
    parent_contact: Optional[str] = None
    emergency_contact: Optional[str] = None
    emergency_contact_phone: Optional[str] = None
    
    status: StudentHostelStatus = StudentHostelStatus.ACTIVE
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class StudentHostelCreate(SQLModel):
    student_id: str
    hostel_id: str
    room_id: Optional[str] = None
    check_in_date: str
    academic_year: str
    parent_contact: Optional[str] = None
    emergency_contact: Optional[str] = None
    emergency_contact_phone: Optional[str] = None
    notes: Optional[str] = None


class StudentHostelUpdate(SQLModel):
    room_id: Optional[str] = None
    check_out_date: Optional[str] = None
    parent_contact: Optional[str] = None
    emergency_contact: Optional[str] = None
    emergency_contact_phone: Optional[str] = None
    status: Optional[StudentHostelStatus] = None
    notes: Optional[str] = None


class RoomAllocation(SQLModel, table=True):
    """Track room allocations to students"""
    __tablename__ = "room_allocations"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)
    room_id: str = Field(sa_column=Column(String, ForeignKey("rooms.id", ondelete="CASCADE"), index=True))
    hostel_id: str = Field(sa_column=Column(String, ForeignKey("hostels.id", ondelete="CASCADE"), index=True))

    allocation_date: str
    deallocation_date: Optional[str] = None
    bed_number: Optional[str] = None
    academic_year: str
    
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class RoomAllocationCreate(SQLModel):
    student_id: str
    room_id: str
    hostel_id: str
    allocation_date: str
    academic_year: str
    bed_number: Optional[str] = None
    notes: Optional[str] = None


class HostelAttendance(SQLModel, table=True):
    """Hostel check-in/check-out attendance"""
    __tablename__ = "hostel_attendance"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)
    hostel_id: str = Field(sa_column=Column(String, ForeignKey("hostels.id", ondelete="CASCADE"), index=True))

    attendance_date: str
    check_in_time: Optional[str] = None
    check_out_time: Optional[str] = None
    status: CheckInStatus = CheckInStatus.CHECKED_IN
    
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class HostelAttendanceCreate(SQLModel):
    student_id: str
    hostel_id: str
    attendance_date: str
    check_in_time: Optional[str] = None
    check_out_time: Optional[str] = None
    status: CheckInStatus = CheckInStatus.CHECKED_IN
    notes: Optional[str] = None


class HostelFee(SQLModel, table=True):
    """Hostel accommodation fees"""
    __tablename__ = "hostel_fees"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)
    hostel_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("hostels.id", ondelete="SET NULL"), index=True)
    )
    academic_term_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("academic_terms.id", ondelete="SET NULL"), index=True)
    )
    fee_structure_id: Optional[str] = Field(default=None, index=True)

    fee_type: HostelFeeType
    amount_due: float
    amount_paid: float = 0.0
    discount: float = 0.0
    
    # Payment details
    payment_date: Optional[str] = None
    payment_method: Optional[str] = None
    receipt_number: Optional[str] = None
    
    # GL Posting
    gl_journal_entry_id: Optional[str] = None
    gl_posted_date: Optional[str] = None
    
    # Status tracking
    is_paid: bool = False
    due_date: Optional[str] = None
    
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class HostelFeeCreate(SQLModel):
    student_id: str
    hostel_id: str
    academic_term_id: Optional[str] = None
    fee_structure_id: Optional[str] = None
    fee_type: HostelFeeType
    amount_due: float
    discount: float = 0.0
    due_date: Optional[str] = None
    notes: Optional[str] = None


class HostelFeeUpdate(SQLModel):
    amount_due: Optional[float] = None
    amount_paid: Optional[float] = None
    discount: Optional[float] = None
    payment_date: Optional[str] = None
    payment_method: Optional[str] = None
    receipt_number: Optional[str] = None
    is_paid: Optional[bool] = None
    gl_journal_entry_id: Optional[str] = None
    gl_posted_date: Optional[str] = None
    notes: Optional[str] = None


class HostelFeeStructure(SQLModel, table=True):
    """Fee structure for hostel billing"""
    __tablename__ = "hostel_fee_structures"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    hostel_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("hostels.id", ondelete="SET NULL"), index=True)
    )
    academic_term_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("academic_terms.id", ondelete="SET NULL"), index=True)
    )
    
    fee_type: HostelFeeType  # MONTHLY, TERM, ANNUAL, SEMESTER
    amount: float
    description: Optional[str] = None
    due_date: Optional[str] = None
    
    # GL Account mapping for auto-posting
    gl_revenue_account_code: Optional[str] = None  # e.g., "4100" for hostel revenue
    gl_receivable_account_code: Optional[str] = None  # e.g., "1200" for student receivables
    
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class HostelFeeStructureCreate(SQLModel):
    hostel_id: str
    academic_term_id: Optional[str] = None
    fee_type: HostelFeeType
    amount: float
    description: Optional[str] = None
    due_date: Optional[str] = None
    gl_revenue_account_code: Optional[str] = None
    gl_receivable_account_code: Optional[str] = None
    is_active: bool = True


class HostelFeeStructureUpdate(SQLModel):
    amount: Optional[float] = None
    description: Optional[str] = None
    due_date: Optional[str] = None
    gl_revenue_account_code: Optional[str] = None
    gl_receivable_account_code: Optional[str] = None
    is_active: Optional[bool] = None


class HostelMaintenance(SQLModel, table=True):
    """Hostel/Room maintenance record"""
    __tablename__ = "hostel_maintenance"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    hostel_id: str = Field(sa_column=Column(String, ForeignKey("hostels.id", ondelete="CASCADE"), index=True))
    room_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("rooms.id", ondelete="SET NULL"), index=True)
    )

    maintenance_date: str
    maintenance_type: str  # e.g., "Cleaning", "Repair", "Inspection"
    description: str
    cost: float = 0.0
    status: str = "completed"  # pending, completed, cancelled
    # Optional links into the general facilities module — same reasoning
    # as VehicleMaintenance.contractor_id/work_order_id in models/transport.py.
    contractor_id: Optional[str] = Field(default=None, index=True)
    work_order_id: Optional[str] = Field(default=None, index=True)

    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class HostelMaintenanceCreate(SQLModel):
    hostel_id: str
    room_id: Optional[str] = None
    maintenance_date: str
    maintenance_type: str
    description: str
    cost: float = 0.0
    status: str = "completed"
    contractor_id: Optional[str] = None
    work_order_id: Optional[str] = None
    notes: Optional[str] = None


class RoomInventoryItem(SQLModel, table=True):
    """Per-item condition/inventory log for a room (furniture, fixtures,
    appliances, etc.) — distinct from the boolean amenity flags on Room,
    which only record whether a facility exists, not its condition."""
    __tablename__ = "room_inventory_items"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    room_id: str = Field(sa_column=Column(String, ForeignKey("rooms.id", ondelete="CASCADE"), index=True))
    item_name: str
    condition: str = "good"
    last_checked_date: str
    checked_by: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class RoomInventoryItemCreate(SQLModel):
    item_name: str
    condition: str = "good"
    last_checked_date: str
    checked_by: Optional[str] = None
    notes: Optional[str] = None


class RoomInventoryItemUpdate(SQLModel):
    item_name: Optional[str] = None
    condition: Optional[str] = None
    last_checked_date: Optional[str] = None
    checked_by: Optional[str] = None
    notes: Optional[str] = None


class HostelVisitor(SQLModel, table=True):
    """Record of hostel visitors"""
    __tablename__ = "hostel_visitors"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    hostel_id: str = Field(sa_column=Column(String, ForeignKey("hostels.id", ondelete="CASCADE"), index=True))
    student_id: str = Field(index=True)

    visitor_name: str
    visitor_phone: Optional[str] = None
    relationship: str  # Parent, Guardian, Friend, etc.

    id_type: Optional[str] = None  # e.g. "National ID", "Passport", "Driver's License"
    id_number: Optional[str] = None

    visit_date: str
    check_in_time: Optional[str] = None
    check_out_time: Optional[str] = None

    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class HostelVisitorCreate(SQLModel):
    student_id: str
    hostel_id: str
    visitor_name: str
    visitor_phone: Optional[str] = None
    relationship: str
    id_type: Optional[str] = None
    id_number: Optional[str] = None
    visit_date: str
    check_in_time: Optional[str] = None
    check_out_time: Optional[str] = None
    notes: Optional[str] = None


class HostelVisitorUpdate(SQLModel):
    visitor_name: Optional[str] = None
    visitor_phone: Optional[str] = None
    relationship: Optional[str] = None
    id_type: Optional[str] = None
    id_number: Optional[str] = None
    visit_date: Optional[str] = None
    check_in_time: Optional[str] = None
    check_out_time: Optional[str] = None
    notes: Optional[str] = None


class HostelComplaint(SQLModel, table=True):
    """Hostel-related complaints/issues"""
    __tablename__ = "hostel_complaints"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    hostel_id: str = Field(sa_column=Column(String, ForeignKey("hostels.id", ondelete="CASCADE"), index=True))
    student_id: str = Field(index=True)
    room_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("rooms.id", ondelete="SET NULL"), index=True)
    )

    complaint_type: str  # Maintenance, Noise, Cleanliness, etc.
    title: str
    description: str
    
    status: str = "open"  # open, resolved, pending
    priority: str = "normal"  # low, normal, high, urgent
    
    reported_date: str
    resolved_date: Optional[str] = None
    resolution_notes: Optional[str] = None
    
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class HostelComplaintCreate(SQLModel):
    hostel_id: str
    student_id: str
    room_id: Optional[str] = None
    complaint_type: str
    title: str
    description: str
    priority: str = "normal"
    reported_date: str
    resolution_notes: Optional[str] = None


class HostelComplaintUpdate(SQLModel):
    status: Optional[str] = None
    priority: Optional[str] = None
    resolution_notes: Optional[str] = None
    resolved_date: Optional[str] = None
