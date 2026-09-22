"""Transport Management Models"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey, UniqueConstraint
from typing import Optional, List
from datetime import datetime
from enum import Enum
import uuid


class VehicleStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    MAINTENANCE = "maintenance"
    RETIRED = "retired"


class VehicleType(str, Enum):
    BUS = "bus"
    VAN = "van"
    MINIBUS = "minibus"
    SHUTTLE = "shuttle"


class RouteStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"


class TransportFeeType(str, Enum):
    MONTHLY = "monthly"
    TERM = "term"
    ANNUAL = "annual"
    PER_TRIP = "per_trip"


class AttendanceStatus(str, Enum):
    PRESENT = "present"
    ABSENT = "absent"
    LATE = "late"
    EXCUSED = "excused"


class Vehicle(SQLModel, table=True):
    """Vehicle/Bus model"""
    __tablename__ = "vehicles"
    __table_args__ = (UniqueConstraint("school_id", "registration_number", name="uq_vehicles_school_registration"),)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    # Registration numbers are only guaranteed unique within a school's own
    # fleet, not globally -- two different schools (or a shared contractor
    # serving both) can legitimately record the same plate. See
    # uq_vehicles_school_registration above.
    registration_number: str = Field(index=True)
    vehicle_type: VehicleType
    make: str
    model: str
    year: int
    color: Optional[str] = None
    seating_capacity: int
    current_occupancy: int = 0
    driver_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("driver_staff.id", ondelete="SET NULL"), index=True)
    )
    conductor_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("driver_staff.id", ondelete="SET NULL"), index=True)
    )

    # Maintenance tracking
    last_service_date: Optional[str] = None
    next_service_date: Optional[str] = None
    insurance_expiry: Optional[str] = None
    roadworthiness_expiry: Optional[str] = None
    
    status: VehicleStatus = VehicleStatus.ACTIVE
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class VehicleCreate(SQLModel):
    registration_number: str
    vehicle_type: VehicleType
    make: str
    model: str
    year: int
    color: Optional[str] = None
    seating_capacity: int
    driver_id: Optional[str] = None
    conductor_id: Optional[str] = None
    last_service_date: Optional[str] = None
    next_service_date: Optional[str] = None
    insurance_expiry: Optional[str] = None
    roadworthiness_expiry: Optional[str] = None
    notes: Optional[str] = None


class VehicleUpdate(SQLModel):
    registration_number: Optional[str] = None
    vehicle_type: Optional[VehicleType] = None
    make: Optional[str] = None
    model: Optional[str] = None
    year: Optional[int] = None
    color: Optional[str] = None
    seating_capacity: Optional[int] = None
    driver_id: Optional[str] = None
    conductor_id: Optional[str] = None
    last_service_date: Optional[str] = None
    next_service_date: Optional[str] = None
    insurance_expiry: Optional[str] = None
    roadworthiness_expiry: Optional[str] = None
    status: Optional[VehicleStatus] = None
    notes: Optional[str] = None


class Route(SQLModel, table=True):
    """Route model"""
    __tablename__ = "routes"
    __table_args__ = (UniqueConstraint("school_id", "route_code", name="uq_routes_school_route_code"),)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    route_name: str = Field(index=True)
    start_point: str
    end_point: str
    # Route codes are a school's own internal labeling scheme, not a
    # globally unique identifier -- see uq_routes_school_route_code above.
    route_code: str = Field(index=True)
    distance_km: float
    estimated_duration_minutes: int
    vehicle_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("vehicles.id", ondelete="SET NULL"), index=True)
    )
    
    # Schedule
    pickup_time: str  # HH:MM format
    dropoff_time: str  # HH:MM format
    pickup_days: str  # JSON string of days (e.g., "['Monday', 'Tuesday', ...]")
    
    # Route details
    intermediate_stops: Optional[str] = None  # JSON string of stop names
    student_count: int = 0
    fee_amount: float
    status: RouteStatus = RouteStatus.ACTIVE
    notes: Optional[str] = None
    # Arrival geofence — optional. When all three are set, a GPS ping within
    # arrival_geofence_meters of (destination_lat, destination_lng) in
    # routers/security.py::post_bus_location auto-transitions every student
    # still EN_ROUTE_BUS on this route to ARRIVED_UNCONFIRMED, instead of
    # requiring a staff member to notice the bus arrived and tap a bulk/
    # per-student action. Left unset, a route works exactly as before —
    # arrival still requires POST /security/transport/{route_id}/arrived.
    destination_lat: Optional[float] = None
    destination_lng: Optional[float] = None
    arrival_geofence_meters: Optional[float] = None
    # Speed alerting — optional. When set, a GPS ping reporting speed_kmh
    # above this raises a SecurityIncident + admin SMS (debounced — see
    # routers/security.py::post_bus_location). Left unset, no alerting.
    max_speed_kmh: Optional[float] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class RouteCreate(SQLModel):
    route_name: str
    start_point: str
    end_point: str
    route_code: str
    distance_km: float
    estimated_duration_minutes: int
    vehicle_id: Optional[str] = None
    pickup_time: str
    dropoff_time: str
    pickup_days: str
    intermediate_stops: Optional[str] = None
    fee_amount: float
    notes: Optional[str] = None
    destination_lat: Optional[float] = None
    destination_lng: Optional[float] = None
    arrival_geofence_meters: Optional[float] = None
    max_speed_kmh: Optional[float] = None


class RouteUpdate(SQLModel):
    route_name: Optional[str] = None
    start_point: Optional[str] = None
    end_point: Optional[str] = None
    route_code: Optional[str] = None
    distance_km: Optional[float] = None
    estimated_duration_minutes: Optional[int] = None
    vehicle_id: Optional[str] = None
    pickup_time: Optional[str] = None
    dropoff_time: Optional[str] = None
    pickup_days: Optional[str] = None
    intermediate_stops: Optional[str] = None
    fee_amount: Optional[float] = None
    status: Optional[RouteStatus] = None
    notes: Optional[str] = None
    destination_lat: Optional[float] = None
    destination_lng: Optional[float] = None
    arrival_geofence_meters: Optional[float] = None
    max_speed_kmh: Optional[float] = None


class RouteStop(SQLModel, table=True):
    """An ordered, GPS-coordinate-bearing stop on a route — deliberately a
    SEPARATE table from Route.intermediate_stops (a flat JSON list of stop
    NAMES, read by existing route CRUD and the frontend as plain strings).
    Redefining that column's meaning to carry coordinates would be a
    breaking change to every existing consumer; this is purely additive —
    a route with no RouteStop rows behaves exactly as it always has.
    Lets a live GPS ping (routers/security.py::post_bus_location) be turned
    into a per-stop distance/ETA instead of only a raw lat/lng on a map."""
    __tablename__ = "route_stops"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    route_id: str = Field(sa_column=Column(String, ForeignKey("routes.id", ondelete="CASCADE"), index=True))

    sequence: int  # 1-based order along the route
    name: str
    lat: float
    lng: float
    # Optional scheduled offset from Route.pickup_time, in minutes — lets a
    # parent see a scheduled ETA even before a driver has gone live with GPS
    # today. Purely informational; the live geofence/ETA logic never reads it.
    eta_offset_minutes: Optional[int] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class RouteStopCreate(SQLModel):
    sequence: int
    name: str
    lat: float
    lng: float
    eta_offset_minutes: Optional[int] = None


class RouteStopUpdate(SQLModel):
    sequence: Optional[int] = None
    name: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    eta_offset_minutes: Optional[int] = None


class BulkRouteStopsRequest(SQLModel):
    """Replaces the ENTIRE ordered stop list for a route in one call — the
    natural shape for a "define this route's stops" admin UI (draw pins on
    a map, save all at once) rather than one create call per stop."""
    stops: List[RouteStopCreate]


class StudentTransport(SQLModel, table=True):
    """Student Transport Enrollment"""
    __tablename__ = "student_transport"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)
    route_id: str = Field(sa_column=Column(String, ForeignKey("routes.id", ondelete="CASCADE"), index=True))

    # Pickup and dropoff points
    pickup_point: Optional[str] = None
    dropoff_point: Optional[str] = None
    
    # Contact info
    emergency_contact: Optional[str] = None
    emergency_contact_phone: Optional[str] = None
    
    is_active: bool = True
    enrollment_date: str
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class StudentTransportCreate(SQLModel):
    student_id: str
    route_id: str
    pickup_point: Optional[str] = None
    dropoff_point: Optional[str] = None
    emergency_contact: Optional[str] = None
    emergency_contact_phone: Optional[str] = None
    enrollment_date: str
    notes: Optional[str] = None


class StudentTransportUpdate(SQLModel):
    route_id: Optional[str] = None
    pickup_point: Optional[str] = None
    dropoff_point: Optional[str] = None
    emergency_contact: Optional[str] = None
    emergency_contact_phone: Optional[str] = None
    is_active: Optional[bool] = None
    notes: Optional[str] = None


class TransportAttendance(SQLModel, table=True):
    """Daily transport attendance tracking"""
    __tablename__ = "transport_attendance"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    route_id: str = Field(sa_column=Column(String, ForeignKey("routes.id", ondelete="CASCADE"), index=True))
    student_id: str = Field(index=True)
    vehicle_id: str = Field(sa_column=Column(String, ForeignKey("vehicles.id", ondelete="CASCADE"), index=True))

    attendance_date: str  # DATE format YYYY-MM-DD
    status: AttendanceStatus = AttendanceStatus.PRESENT
    
    # Trip information
    trip_type: str  # 'pickup' or 'dropoff'
    timestamp: Optional[str] = None  # ISO format datetime
    
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class TransportAttendanceCreate(SQLModel):
    route_id: str
    student_id: str
    vehicle_id: str
    attendance_date: str
    status: AttendanceStatus = AttendanceStatus.PRESENT
    trip_type: str
    timestamp: Optional[str] = None
    notes: Optional[str] = None


class TransportAttendanceBulk(SQLModel):
    """Bulk attendance submission"""
    attendance_records: List[TransportAttendanceCreate]


class TransportFee(SQLModel, table=True):
    """Transport fees for students"""
    __tablename__ = "transport_fees"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)
    route_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("routes.id", ondelete="SET NULL"), index=True)
    )
    academic_term_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("academic_terms.id", ondelete="SET NULL"), index=True)
    )

    fee_type: TransportFeeType
    amount_due: float
    amount_paid: float = 0.0
    discount: float = 0.0
    
    # Payment details
    payment_date: Optional[str] = None
    payment_method: Optional[str] = None
    receipt_number: Optional[str] = None
    
    # Status tracking
    is_paid: bool = False
    due_date: Optional[str] = None
    
    # GL Integration (auto-posting)
    gl_journal_entry_id: Optional[str] = None
    gl_posted_date: Optional[str] = None
    
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class TransportFeeCreate(SQLModel):
    student_id: str
    route_id: str
    academic_term_id: Optional[str] = None
    fee_type: TransportFeeType
    amount_due: float
    amount_paid: float = 0.0
    discount: float = 0.0
    payment_date: Optional[str] = None
    payment_method: Optional[str] = None
    receipt_number: Optional[str] = None
    due_date: Optional[str] = None
    notes: Optional[str] = None


class TransportFeeUpdate(SQLModel):
    amount_due: Optional[float] = None
    amount_paid: Optional[float] = None
    discount: Optional[float] = None
    payment_date: Optional[str] = None
    payment_method: Optional[str] = None
    receipt_number: Optional[str] = None
    is_paid: Optional[bool] = None
    notes: Optional[str] = None


class VehicleMaintenance(SQLModel, table=True):
    """Vehicle maintenance record"""
    __tablename__ = "vehicle_maintenance"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    vehicle_id: str = Field(sa_column=Column(String, ForeignKey("vehicles.id", ondelete="CASCADE"), index=True))

    maintenance_date: str
    maintenance_type: str  # e.g., "Oil Change", "Tire Replacement", "Inspection"
    description: str
    cost: float
    # Optional links into the general facilities module, so a vehicle
    # service can be done by a tracked FacilityContractor and/or tied to a
    # FacilityWorkOrder for shared service-history reporting — this log
    # stays the source of truth for vehicle service either way.
    contractor_id: Optional[str] = Field(default=None, index=True)
    work_order_id: Optional[str] = Field(default=None, index=True)
    notes: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)


class VehicleMaintenanceCreate(SQLModel):
    vehicle_id: str
    maintenance_date: str
    maintenance_type: str
    description: str
    cost: float
    contractor_id: Optional[str] = None
    work_order_id: Optional[str] = None
    notes: Optional[str] = None


class DriverStaff(SQLModel, table=True):
    """Driver and Conductor staff assignment"""
    __tablename__ = "driver_staff"
    __table_args__ = (UniqueConstraint("school_id", "license_number", name="uq_driver_staff_school_license"),)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    # staff_id references Staff.id, already a globally-unique UUID PK --
    # this unique=True is a correct "one driver profile per Staff row"
    # guard, not a natural key, so it's left as a plain global constraint.
    staff_id: str = Field(unique=True, index=True)  # References Staff model

    # License numbers can legitimately repeat across schools (a driver
    # licensed once can drive for more than one school, or a contractor
    # shares staff across schools) -- see uq_driver_staff_school_license.
    license_number: str = Field(index=True)
    license_expiry: str
    role: str  # 'driver' or 'conductor'
    
    # Insurance details
    insurance_provider: Optional[str] = None
    insurance_policy_number: Optional[str] = None
    insurance_expiry: Optional[str] = None
    
    is_active: bool = True
    is_verified: bool = False
    verification_date: Optional[str] = None
    
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class DriverStaffCreate(SQLModel):
    staff_id: str
    license_number: str
    license_expiry: str
    role: str
    insurance_provider: Optional[str] = None
    insurance_policy_number: Optional[str] = None
    insurance_expiry: Optional[str] = None
    notes: Optional[str] = None


class DriverStaffUpdate(SQLModel):
    license_number: Optional[str] = None
    license_expiry: Optional[str] = None
    role: Optional[str] = None
    insurance_provider: Optional[str] = None
    insurance_policy_number: Optional[str] = None
    insurance_expiry: Optional[str] = None
    is_active: Optional[bool] = None
    is_verified: Optional[bool] = None
    verification_date: Optional[str] = None
    notes: Optional[str] = None
