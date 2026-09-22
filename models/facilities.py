"""School facilities, assets, maintenance requests, and preventive schedules."""
from datetime import datetime
from enum import Enum
from typing import Optional
import uuid

from sqlmodel import Field, SQLModel


class MaintenanceStatus(str, Enum):
    OPEN = "open"
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class FacilityAsset(SQLModel, table=True):
    __tablename__ = "facility_assets"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    name: str
    asset_tag: Optional[str] = Field(default=None, index=True)
    category: str = "general"
    location: Optional[str] = None
    condition: str = "good"
    status: str = "active"
    # Cross-reference to models.inventory.Asset, the general durable-asset
    # register — deliberately not a merge of the two tables (that's a much
    # bigger migration touching every consumer of either one). This just
    # lets a facility asset point at its inventory twin when one exists,
    # same reasoning as FacilityContractor.supplier_id below.
    inventory_asset_id: Optional[str] = Field(default=None, index=True)
    purchase_date: Optional[str] = None
    purchase_cost: Optional[float] = None
    warranty_end: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class FacilityAssetCreate(SQLModel):
    name: str
    asset_tag: Optional[str] = None
    category: str = "general"
    location: Optional[str] = None
    condition: str = "good"
    status: str = "active"
    inventory_asset_id: Optional[str] = None
    purchase_date: Optional[str] = None
    purchase_cost: Optional[float] = None
    warranty_end: Optional[str] = None
    notes: Optional[str] = None


class MaintenanceRequest(SQLModel, table=True):
    __tablename__ = "facility_maintenance_requests"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    asset_id: Optional[str] = Field(default=None, index=True)
    # Set once this fault report has been turned into a real work order —
    # see POST /requests/{id}/convert-to-work-order. Null means "reported,
    # not yet actioned."
    work_order_id: Optional[str] = Field(default=None, index=True)
    title: str
    description: str
    priority: str = "normal"
    status: MaintenanceStatus = MaintenanceStatus.OPEN
    requested_by: str = Field(index=True)
    assigned_to: Optional[str] = None
    requested_date: str
    scheduled_date: Optional[str] = None
    completed_date: Optional[str] = None
    estimated_cost: Optional[float] = None
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class MaintenanceRequestCreate(SQLModel):
    asset_id: Optional[str] = None
    title: str
    description: str
    priority: str = "normal"
    requested_date: str
    scheduled_date: Optional[str] = None
    assigned_to: Optional[str] = None
    estimated_cost: Optional[float] = None
    notes: Optional[str] = None


class MaintenanceUpdate(SQLModel):
    status: Optional[MaintenanceStatus] = None
    assigned_to: Optional[str] = None
    scheduled_date: Optional[str] = None
    completed_date: Optional[str] = None
    estimated_cost: Optional[float] = None
    notes: Optional[str] = None


class MaintenanceSchedule(SQLModel, table=True):
    __tablename__ = "facility_maintenance_schedules"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    asset_id: str = Field(index=True)
    room_id: Optional[str] = Field(default=None, index=True)
    contractor_id: Optional[str] = Field(default=None, index=True)
    title: str
    frequency: str
    next_due_date: str
    last_completed_date: Optional[str] = None
    assigned_to: Optional[str] = None
    active: bool = True
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class MaintenanceScheduleCreate(SQLModel):
    asset_id: str
    room_id: Optional[str] = None
    contractor_id: Optional[str] = None
    title: str
    frequency: str
    next_due_date: str
    last_completed_date: Optional[str] = None
    assigned_to: Optional[str] = None
    active: bool = True
    notes: Optional[str] = None


class FacilityAssetUpdate(SQLModel):
    name: Optional[str] = None
    asset_tag: Optional[str] = None
    category: Optional[str] = None
    location: Optional[str] = None
    condition: Optional[str] = None
    status: Optional[str] = None
    inventory_asset_id: Optional[str] = None
    purchase_date: Optional[str] = None
    purchase_cost: Optional[float] = None
    warranty_end: Optional[str] = None
    notes: Optional[str] = None


class MaintenanceScheduleUpdate(SQLModel):
    room_id: Optional[str] = None
    contractor_id: Optional[str] = None
    title: Optional[str] = None
    frequency: Optional[str] = None
    next_due_date: Optional[str] = None
    last_completed_date: Optional[str] = None
    assigned_to: Optional[str] = None
    active: Optional[bool] = None
    notes: Optional[str] = None


class FacilityBuilding(SQLModel, table=True):
    __tablename__ = "facility_buildings"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    name: str
    code: Optional[str] = Field(default=None, index=True)
    address: Optional[str] = None
    floors: int = 1
    status: str = "active"
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class FacilityBuildingCreate(SQLModel):
    name: str
    code: Optional[str] = None
    address: Optional[str] = None
    floors: int = 1
    status: str = "active"
    notes: Optional[str] = None


class FacilityBuildingUpdate(SQLModel):
    name: Optional[str] = None
    code: Optional[str] = None
    address: Optional[str] = None
    floors: Optional[int] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class FacilityRoom(SQLModel, table=True):
    __tablename__ = "facility_rooms"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    building_id: str = Field(index=True)
    name: str
    code: Optional[str] = Field(default=None, index=True)
    room_type: str = "classroom"
    floor: int = 1
    capacity: int = 0
    status: str = "available"
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class FacilityRoomCreate(SQLModel):
    building_id: str
    name: str
    code: Optional[str] = None
    room_type: str = "classroom"
    floor: int = 1
    capacity: int = 0
    status: str = "available"
    notes: Optional[str] = None


class FacilityRoomUpdate(SQLModel):
    building_id: Optional[str] = None
    name: Optional[str] = None
    code: Optional[str] = None
    room_type: Optional[str] = None
    floor: Optional[int] = None
    capacity: Optional[int] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class FacilityEquipment(SQLModel, table=True):
    __tablename__ = "facility_equipment"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    asset_id: Optional[str] = Field(default=None, index=True)
    room_id: Optional[str] = Field(default=None, index=True)
    name: str
    equipment_type: str = "classroom"
    serial_number: Optional[str] = Field(default=None, index=True)
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    condition: str = "good"
    status: str = "active"
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class FacilityEquipmentCreate(SQLModel):
    name: str
    asset_id: Optional[str] = None
    room_id: Optional[str] = None
    equipment_type: str = "classroom"
    serial_number: Optional[str] = None
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    condition: str = "good"
    status: str = "active"
    notes: Optional[str] = None


class FacilityEquipmentUpdate(SQLModel):
    name: Optional[str] = None
    asset_id: Optional[str] = None
    room_id: Optional[str] = None
    equipment_type: Optional[str] = None
    serial_number: Optional[str] = None
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    condition: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class FacilityContractor(SQLModel, table=True):
    __tablename__ = "facility_contractors"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    name: str
    trade: Optional[str] = None
    contact_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    status: str = "active"
    # Cross-reference to models.procurement.Supplier when this maintenance
    # contractor is also a registered procurement supplier — kept as a
    # separate table (not merged) since the two serve different workflows,
    # but this lets facilities and procurement both point at the same
    # real-world vendor. Same reasoning as FacilityAsset.inventory_asset_id.
    supplier_id: Optional[str] = Field(default=None, index=True)
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class FacilityContractorCreate(SQLModel):
    name: str
    trade: Optional[str] = None
    contact_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    status: str = "active"
    supplier_id: Optional[str] = None
    notes: Optional[str] = None


class FacilityContractorUpdate(FacilityContractorCreate):
    name: Optional[str] = None


class FacilityWorkOrder(SQLModel, table=True):
    __tablename__ = "facility_work_orders"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    asset_id: Optional[str] = Field(default=None, index=True)
    room_id: Optional[str] = Field(default=None, index=True)
    contractor_id: Optional[str] = Field(default=None, index=True)
    # Set when this work order was auto-generated by a due
    # MaintenanceSchedule, rather than raised manually or converted from a
    # MaintenanceRequest — see services/facilities_maintenance_service.py.
    schedule_id: Optional[str] = Field(default=None, index=True)
    title: str
    description: str
    priority: str = "normal"
    status: str = "open"
    requested_by: str = Field(index=True)
    assigned_to: Optional[str] = None
    requested_date: str
    scheduled_date: Optional[str] = None
    completed_date: Optional[str] = None
    estimated_cost: Optional[float] = None
    actual_cost: Optional[float] = None
    resolution_notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class FacilityWorkOrderCreate(SQLModel):
    title: str
    description: str
    asset_id: Optional[str] = None
    room_id: Optional[str] = None
    contractor_id: Optional[str] = None
    priority: str = "normal"
    requested_date: str
    assigned_to: Optional[str] = None
    scheduled_date: Optional[str] = None
    estimated_cost: Optional[float] = None


class FacilityWorkOrderUpdate(SQLModel):
    asset_id: Optional[str] = None
    room_id: Optional[str] = None
    contractor_id: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[str] = None
    status: Optional[str] = None
    assigned_to: Optional[str] = None
    scheduled_date: Optional[str] = None
    completed_date: Optional[str] = None
    estimated_cost: Optional[float] = None
    actual_cost: Optional[float] = None
    resolution_notes: Optional[str] = None


class FacilityServiceRecord(SQLModel, table=True):
    __tablename__ = "facility_service_records"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    work_order_id: Optional[str] = Field(default=None, index=True)
    asset_id: Optional[str] = Field(default=None, index=True)
    room_id: Optional[str] = Field(default=None, index=True)
    contractor_id: Optional[str] = Field(default=None, index=True)
    service_date: str
    service_type: str = "repair"
    description: str
    cost: float = 0
    warranty_until: Optional[str] = None
    notes: Optional[str] = None
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class FacilityServiceRecordCreate(SQLModel):
    service_date: str
    service_type: str = "repair"
    description: str
    work_order_id: Optional[str] = None
    asset_id: Optional[str] = None
    room_id: Optional[str] = None
    contractor_id: Optional[str] = None
    cost: float = 0
    warranty_until: Optional[str] = None
    notes: Optional[str] = None


class UtilityMeter(SQLModel, table=True):
    __tablename__ = "facility_utility_meters"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    building_id: Optional[str] = Field(default=None, index=True)
    room_id: Optional[str] = Field(default=None, index=True)
    utility_type: str
    meter_number: str = Field(index=True)
    unit: str = "kWh"
    provider: Optional[str] = None
    status: str = "active"
    created_at: datetime = Field(default_factory=datetime.utcnow)


class UtilityMeterCreate(SQLModel):
    utility_type: str
    meter_number: str
    unit: str = "kWh"
    building_id: Optional[str] = None
    room_id: Optional[str] = None
    provider: Optional[str] = None
    status: str = "active"


class UtilityReading(SQLModel, table=True):
    __tablename__ = "facility_utility_readings"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    meter_id: str = Field(index=True)
    reading_date: str
    reading_value: float
    consumption: Optional[float] = None
    cost: Optional[float] = None
    notes: Optional[str] = None
    recorded_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class UtilityReadingCreate(SQLModel):
    meter_id: str
    reading_date: str
    reading_value: float
    consumption: Optional[float] = None
    cost: Optional[float] = None
    notes: Optional[str] = None


class FacilityBooking(SQLModel, table=True):
    __tablename__ = "facility_bookings"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    room_id: str = Field(index=True)
    title: str
    purpose: Optional[str] = None
    booked_by: str = Field(index=True)
    start_at: str
    end_at: str
    status: str = "confirmed"
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class FacilityBookingCreate(SQLModel):
    room_id: str
    title: str
    start_at: str
    end_at: str
    purpose: Optional[str] = None
    status: str = "confirmed"
    notes: Optional[str] = None


class FacilityBookingUpdate(SQLModel):
    title: Optional[str] = None
    purpose: Optional[str] = None
    start_at: Optional[str] = None
    end_at: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class SafetyInspection(SQLModel, table=True):
    __tablename__ = "facility_safety_inspections"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    building_id: Optional[str] = Field(default=None, index=True)
    room_id: Optional[str] = Field(default=None, index=True)
    inspector_id: str = Field(index=True)
    inspection_type: str = "general"
    inspection_date: str
    next_due_date: Optional[str] = None
    status: str = "passed"
    findings: Optional[str] = None
    corrective_action: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SafetyInspectionCreate(SQLModel):
    inspection_date: str
    inspection_type: str = "general"
    building_id: Optional[str] = None
    room_id: Optional[str] = None
    next_due_date: Optional[str] = None
    status: str = "passed"
    findings: Optional[str] = None
    corrective_action: Optional[str] = None


class SafetyInspectionUpdate(SQLModel):
    inspection_date: Optional[str] = None
    inspection_type: Optional[str] = None
    next_due_date: Optional[str] = None
    status: Optional[str] = None
    findings: Optional[str] = None
    corrective_action: Optional[str] = None