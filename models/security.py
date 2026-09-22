"""Student Security Module Models"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey, UniqueConstraint
from typing import Optional
from datetime import datetime
from enum import Enum
import uuid


class ArrivalStatus(str, Enum):
    PENDING = "pending"
    EN_ROUTE_BUS = "en_route_bus"
    EN_ROUTE_COLLECTOR = "en_route_collector"
    ARRIVED_UNCONFIRMED = "arrived_unconfirmed"
    SAFE_CONFIRMED = "safe_confirmed"


class QRTokenType(str, Enum):
    PARENT_PICKUP = "parent_pickup"
    TRANSPORT_DISPATCH = "transport_dispatch"
    COLLECTOR_SHARE = "collector_share"


class ScanResult(str, Enum):
    AUTHORIZED = "authorized"
    UNAUTHORIZED = "unauthorized"
    EXPIRED = "expired"
    ALREADY_USED = "already_used"


class ArrivalEventType(str, Enum):
    DISPATCHED = "dispatched"
    EN_ROUTE_BUS = "en_route_bus"
    EN_ROUTE_COLLECTOR = "en_route_collector"
    ARRIVED = "arrived"
    PARENT_CONFIRMED = "parent_confirmed"


# ── Student Security Profile ──────────────────────────────────────────────────

class StudentSecurityProfile(SQLModel, table=True):
    __tablename__ = "student_security_profiles"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    student_id: str = Field(index=True)
    school_id: str = Field(index=True)

    ghana_post_address: Optional[str] = None
    home_lat: Optional[float] = None
    home_lng: Optional[float] = None
    neighbourhood: Optional[str] = None

    pickup_method: str = Field(default="parent")  # "parent" | "transport"
    transport_route_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("routes.id", ondelete="SET NULL"), index=True)
    )

    authorized_pickup_name: Optional[str] = None
    authorized_pickup_phone: Optional[str] = None
    authorized_pickup_relationship: Optional[str] = None

    # Current day state — reset each school day
    arrival_status: ArrivalStatus = Field(default=ArrivalStatus.PENDING)
    arrival_time: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None

    # "On My Way" — a parent's own advance signal that they're heading to
    # school, sent before they've physically arrived/scanned a QR code.
    # Independent of arrival_status (which only changes via a physical QR
    # scan or a staff/driver action) so gate staff get advance notice
    # instead of only reacting once someone is already at the gate. Reset
    # daily alongside arrival_status by reset_profile_if_stale().
    parent_on_the_way: bool = False
    parent_on_the_way_at: Optional[datetime] = None
    parent_eta_minutes: Optional[int] = None
    # Set when a QR scan verifies the collector has physically arrived at
    # the gate (routers/security.py's verify_qr_token) — distinct from
    # arrival_time, which is the STUDENT's own status timestamp, not the
    # car's. Powers the pickup queue/"who's waiting" drive-through view.
    parent_arrived_at: Optional[datetime] = None
    queue_position: Optional[int] = None

    # Campus-exit hold: while set, hard-blocks release via BOTH QR scan
    # (verify_qr_token) and staff manual status change (update_student_status)
    # — the only way to release a held student is for an admin/security
    # officer to first lift the hold via its own dedicated endpoint, so
    # lifting it is its own auditable action, not a side effect of a normal
    # pickup action. Does NOT reset daily — a hold stays active until
    # someone deliberately clears it.
    release_hold: bool = False
    release_hold_reason: Optional[str] = None
    release_hold_set_by: Optional[str] = None
    release_hold_set_at: Optional[datetime] = None

    # Real-time on-campus location — who last updated it and when. Defaults
    # to the student's own classroom when attendance is marked; staff can
    # override for ad-hoc movement (nurse's office, library, field trip
    # meeting point, etc.). History lives in StudentLocationLog below.
    current_location: Optional[str] = None
    current_location_updated_at: Optional[datetime] = None
    current_location_updated_by: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AuthorizedPickupPerson(SQLModel, table=True):
    """A named individual pre-approved to collect a student — replaces the
    old single flat authorized_pickup_name/phone/relationship fields above
    (kept on StudentSecurityProfile for backward compatibility, no longer
    the primary record) with a real list, since a student can have more
    than one approved collector (both grandparents, a driver, a neighbour,
    etc.). is_active lets a person be revoked without losing the audit
    record of who was once authorized. Shown to the scanning officer at
    verify_qr_token time so they can visually cross-check ID against it —
    this codebase has no biometric/photo-ID hardware integration, so
    "verification" here means giving staff the reference data (including
    a photo, when uploaded — see routers/security.py's
    upload_pickup_person_photo) to check against, not an automated
    identity match. photo_url also lets this person be issued a printable
    ID card (see models.certificates.PersonType.PICKUP_PERSON)."""
    __tablename__ = "authorized_pickup_persons"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)
    name: str
    phone: Optional[str] = None
    relationship: Optional[str] = None
    notes: Optional[str] = None  # e.g. "Always carries a work ID badge"
    photo_url: Optional[str] = None
    # When true, this person cannot be selected at a gate scan
    # (routers/security.py's verify_qr_token, via person_id) until a photo
    # is on file — a simple, low-cost safeguard for a newly-added collector
    # nobody at the gate would otherwise recognize by name alone.
    photo_required: bool = False
    is_active: bool = True
    added_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AuthorizedPickupPersonCreate(SQLModel):
    name: str
    phone: Optional[str] = None
    relationship: Optional[str] = None
    notes: Optional[str] = None
    photo_required: bool = False


class AuthorizedPickupPersonUpdate(SQLModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    relationship: Optional[str] = None
    notes: Optional[str] = None
    is_active: Optional[bool] = None
    photo_required: Optional[bool] = None


class StudentLocationLog(SQLModel, table=True):
    """History of on-campus location changes for a student — the audit
    trail behind StudentSecurityProfile.current_location, so "where was
    this student at 10am" can be answered after the fact, not just "where
    are they right now"."""
    __tablename__ = "student_location_logs"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)
    location: str
    set_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class UpdateLocationRequest(SQLModel):
    location: str


class SetReleaseHoldRequest(SQLModel):
    reason: str


class StudentSecurityProfileCreate(SQLModel):
    student_id: str
    school_id: str
    ghana_post_address: Optional[str] = None
    home_lat: Optional[float] = None
    home_lng: Optional[float] = None
    neighbourhood: Optional[str] = None
    pickup_method: str = "parent"
    transport_route_id: Optional[str] = None
    authorized_pickup_name: Optional[str] = None
    authorized_pickup_phone: Optional[str] = None
    authorized_pickup_relationship: Optional[str] = None


class StudentSecurityProfileUpdate(SQLModel):
    ghana_post_address: Optional[str] = None
    home_lat: Optional[float] = None
    home_lng: Optional[float] = None
    neighbourhood: Optional[str] = None
    pickup_method: Optional[str] = None
    transport_route_id: Optional[str] = None
    authorized_pickup_name: Optional[str] = None
    authorized_pickup_phone: Optional[str] = None
    authorized_pickup_relationship: Optional[str] = None
    arrival_status: Optional[ArrivalStatus] = None
    arrival_time: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None


# ── Daily QR Token ────────────────────────────────────────────────────────────

class DailyQRToken(SQLModel, table=True):
    __tablename__ = "daily_qr_tokens"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    token: str = Field(index=True, unique=True)
    token_type: QRTokenType
    school_id: str = Field(index=True)

    student_id: Optional[str] = Field(default=None, index=True)
    route_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("routes.id", ondelete="SET NULL"), index=True)
    )

    # Embedded collector session token — dormant until a successful scan activates it
    collector_session_token: str = Field(default_factory=lambda: str(uuid.uuid4()), unique=True)

    issued_date: str  # YYYY-MM-DD
    expires_at: datetime

    is_used: bool = Field(default=False)
    used_at: Optional[datetime] = None
    used_by_scan_id: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)


# ── Security Scan Log ─────────────────────────────────────────────────────────

class SecurityScanLog(SQLModel, table=True):
    __tablename__ = "security_scan_logs"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)

    token_scanned: str
    scanned_by_id: str = Field(index=True)  # FK → User (security_officer)

    result: ScanResult
    student_id: Optional[str] = Field(default=None, index=True)

    gate_lat: Optional[float] = None
    gate_lng: Optional[float] = None

    # If authorized, this links to the collector session created
    collector_session_id: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)


# ── Arrival Event ─────────────────────────────────────────────────────────────

class ArrivalEvent(SQLModel, table=True):
    __tablename__ = "arrival_events"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)

    event_type: ArrivalEventType
    triggered_by_id: str  # FK → User
    notes: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)


# ── Parent Note ───────────────────────────────────────────────────────────────

class ParentNote(SQLModel, table=True):
    __tablename__ = "parent_notes"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)
    author_id: str  # FK → User (parent)

    text: str
    is_confirmation: bool = Field(default=False)

    created_at: datetime = Field(default_factory=datetime.utcnow)


class ParentNoteCreate(SQLModel):
    text: str
    is_confirmation: bool = False


# ── Live Bus Location ─────────────────────────────────────────────────────────

class LiveBusLocation(SQLModel, table=True):
    __tablename__ = "live_bus_locations"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    route_id: str = Field(sa_column=Column(String, ForeignKey("routes.id", ondelete="CASCADE"), index=True))
    driver_id: str = Field(sa_column=Column(String, ForeignKey("driver_staff.id", ondelete="CASCADE"), index=True))

    lat: float
    lng: float
    speed_kmh: Optional[float] = None
    heading: Optional[float] = None

    recorded_at: datetime = Field(default_factory=datetime.utcnow)


class LiveBusLocationCreate(SQLModel):
    lat: float
    lng: float
    speed_kmh: Optional[float] = None
    heading: Optional[float] = None


# ── Collector Tracking Session ────────────────────────────────────────────────

class CollectorTrackingSession(SQLModel, table=True):
    __tablename__ = "collector_tracking_sessions"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    session_token: str = Field(index=True, unique=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)

    scan_log_id: str  # FK → SecurityScanLog
    collector_name: Optional[str] = None  # Captured at gate if provided

    gate_lat: Optional[float] = None
    gate_lng: Optional[float] = None

    is_active: bool = Field(default=True)
    started_at: datetime = Field(default_factory=datetime.utcnow)
    ended_at: Optional[datetime] = None


# ── Collector Live Location ───────────────────────────────────────────────────

class CollectorLiveLocation(SQLModel, table=True):
    __tablename__ = "collector_live_locations"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    session_id: str = Field(index=True)  # FK → CollectorTrackingSession

    lat: float
    lng: float

    recorded_at: datetime = Field(default_factory=datetime.utcnow)


class CollectorLocationCreate(SQLModel):
    lat: float
    lng: float


# ── School QR Dispatch (idempotent dispatch record) ──────────────────────────

class SchoolQRDispatch(SQLModel, table=True):
    __tablename__ = "school_qr_dispatches"
    __table_args__ = (UniqueConstraint('school_id', 'dispatch_date', name='uix_school_dispatch_date'),)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    dispatch_date: str = Field(index=True)  # YYYY-MM-DD

    dispatched_at: datetime = Field(default_factory=datetime.utcnow)
    dispatched_by: Optional[str] = None
    idempotency_key: Optional[str] = Field(default=None, index=True)
    meta: Optional[str] = None


class SchoolQRDispatchCreate(SQLModel):
    school_id: str
    idempotency_key: Optional[str] = None


# ── Kiosk Login (shared-device e-canteen self-checkout) ──────────────────────
# A student without a personal phone/tablet scans their ID card's QR
# (routers/id_cards.py's card_number) on a school-owned shared tablet, then
# enters a PIN (Student.kiosk_pin_hash) to open a short-lived session scoped
# to e-canteen self-checkout only (auth.py's get_current_user confines it to
# /api/canteen-wallet/*) — see routers/kiosk.py. Modeled on
# CollectorTrackingSession's opaque-DB-token pattern (not a JWT), since
# get_current_user has no `type`-claim enforcement to hook into safely.

class KioskPendingScan(SQLModel, table=True):
    """A card scan that's been matched to a student but not yet PIN-verified.
    Kept separate from KioskSession so PIN attempts (and their lockout) are
    counted per-scan without needing to re-transmit the card number."""
    __tablename__ = "kiosk_pending_scans"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    pending_token: str = Field(index=True, unique=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)
    user_id: str

    pin_attempts: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime


class SecurityIncident(SQLModel, table=True):
    """A persisted, must-be-acknowledged record of a safeguarding-relevant
    event — introduced because custody-restriction denials and unauthorized
    QR scans previously only fired a transient SSE broadcast (+ SMS to the
    parent for unauthorized scans): nothing accumulated a pattern (e.g. the
    same restricted parent trying repeatedly) and nothing required a human
    to ever close the loop, unlike Discipline's maker-checker incident
    workflow. incident_type is a free string (not a native enum, matching
    this codebase's enum-avoidance convention) — expected values so far:
    "custody_restriction_attempt", "custody_restriction_active_scan",
    "unauthorized_scan", "late_pickup_unresolved", "lockdown_activated",
    "lockdown_deactivated"."""
    __tablename__ = "security_incidents"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: Optional[str] = Field(default=None, index=True)
    incident_type: str = Field(index=True)
    details: Optional[str] = None
    related_user_id: Optional[str] = None  # e.g. the parent who was denied, or the officer who scanned

    acknowledged: bool = Field(default=False)
    acknowledged_by: Optional[str] = None
    acknowledged_at: Optional[datetime] = None
    resolution_notes: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)


class AcknowledgeIncidentRequest(SQLModel):
    resolution_notes: Optional[str] = None


class SetLockdownRequest(SQLModel):
    active: bool
    reason: Optional[str] = None


class KioskSession(SQLModel, table=True):
    __tablename__ = "kiosk_sessions"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    session_token: str = Field(index=True, unique=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)
    user_id: str = Field(index=True)

    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime  # absolute cap
    last_seen_at: datetime = Field(default_factory=datetime.utcnow)  # drives idle timeout
    ended_at: Optional[datetime] = None
    is_active: bool = Field(default=True)
