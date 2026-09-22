"""Third-party integration layer: API keys (inbound) and webhooks (outbound).

Additive to the rest of the app — no existing auth path, router, or model
changes. API key `scopes` reuse the same permission-code vocabulary as
models/rbac.py (e.g. "students.record.view"), so a school composing access
for an integrator uses the same catalog as composing a staff role.
"""
from sqlmodel import SQLModel, Field
from sqlalchemy import JSON
from typing import Optional, List
from datetime import datetime
from enum import Enum
import uuid

from models.assignment import AssignmentType, SubmissionStatus


class ApiKey(SQLModel, table=True):
    __tablename__ = "api_keys"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    name: str
    key_prefix: str = Field(index=True)  # first 8 chars of the raw key, shown in UI for identification
    key_hash: str = Field(index=True, unique=True)  # sha256 of the full raw key; raw key is never stored
    scopes: List[str] = Field(default_factory=list, sa_type=JSON)
    is_active: bool = Field(default=True, index=True)
    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_used_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class WebhookEndpoint(SQLModel, table=True):
    __tablename__ = "webhook_endpoints"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    url: str
    secret_encrypted: str  # services/ai_key_crypto.py's Fernet scheme, reused as-is
    subscribed_events: List[str] = Field(default_factory=list, sa_type=JSON)
    is_active: bool = Field(default=True, index=True)
    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class WebhookDelivery(SQLModel, table=True):
    __tablename__ = "webhook_deliveries"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    webhook_endpoint_id: str = Field(index=True)
    event_type: str = Field(index=True)
    payload: str  # JSON-encoded, kept for replay/debugging
    status: str = Field(default="pending", index=True)  # pending | success | failed
    response_code: Optional[int] = None
    attempts: int = Field(default=0)
    last_attempted_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ApiKeyResponse(SQLModel):
    id: str
    name: str
    key_prefix: str
    scopes: List[str]
    is_active: bool
    created_at: datetime
    last_used_at: Optional[datetime]
    expires_at: Optional[datetime]


class ApiKeyCreateResponse(ApiKeyResponse):
    """Only ever returned once, from the create endpoint."""
    raw_key: str


class ApiKeyCreate(SQLModel):
    name: str
    scopes: List[str] = []
    expires_at: Optional[datetime] = None


class WebhookEndpointCreate(SQLModel):
    url: str
    subscribed_events: List[str] = []


class WebhookEndpointUpdate(SQLModel):
    url: Optional[str] = None
    subscribed_events: Optional[List[str]] = None
    is_active: Optional[bool] = None


class WebhookEndpointResponse(SQLModel):
    id: str
    url: str
    subscribed_events: List[str]
    is_active: bool
    created_at: datetime


class WebhookEndpointCreateResponse(WebhookEndpointResponse):
    """Only ever returned once, from the create endpoint."""
    raw_secret: str


class WebhookDeliveryResponse(SQLModel):
    id: str
    event_type: str
    status: str
    response_code: Optional[int]
    attempts: int
    last_attempted_at: Optional[datetime]
    created_at: datetime


class QuickBooksConnection(SQLModel, table=True):
    """One school's OAuth2 connection to a QuickBooks Online company (see
    services/quickbooks_service.py). access_token/refresh_token are
    Fernet-encrypted at rest via services/ai_key_crypto.py, same as
    WebhookEndpoint.secret_encrypted above. QBO refresh tokens rotate on
    every use — both token fields and both expiry fields are overwritten
    on each refresh, never appended."""
    __tablename__ = "quickbooks_connections"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True, unique=True)  # one connection per school
    realm_id: str  # QuickBooks' company identifier
    access_token_encrypted: str
    refresh_token_encrypted: str
    access_token_expires_at: datetime
    refresh_token_expires_at: datetime
    environment: str = "sandbox"  # "sandbox" or "production"
    is_active: bool = Field(default=True, index=True)
    connected_by: Optional[str] = None
    connected_at: datetime = Field(default_factory=datetime.utcnow)
    last_synced_at: Optional[datetime] = None


class QuickBooksConnectionStatus(SQLModel):
    connected: bool
    realm_id: Optional[str] = None
    environment: Optional[str] = None
    last_synced_at: Optional[datetime] = None


class QuickBooksSyncResult(SQLModel):
    created: int
    skipped: int


class QuickBooksAccountMapping(SQLModel, table=True):
    """Maps one of our GL accounts to the QuickBooks Online Account.Id it
    corresponds to, so a pushed journal entry line knows which QBO account
    to post against — QBO's JournalEntry API takes an AccountRef by Id, not
    by name/code, unlike the name-based ExternalAccountMapping used for the
    Tally/QuickBooks Desktop file exports. Populated automatically for
    accounts created via quickbooks_service.sync_chart_of_accounts (the
    pull direction); accounts that existed locally before that first pull
    need this set explicitly via PUT /quickbooks/account-mappings first."""
    __tablename__ = "quickbooks_account_mappings"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    gl_account_id: str = Field(index=True, unique=True)
    qb_account_id: str
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    updated_by: str


class QuickBooksAccountMappingUpsert(SQLModel):
    gl_account_id: str
    qb_account_id: str


class QuickBooksJournalSyncLog(SQLModel, table=True):
    """Idempotency + audit record: which JournalEntry rows have been pushed
    to QuickBooks Online, and the QBO JournalEntry.Id each became — so
    re-running the push for an overlapping date range skips entries already
    sent, and the QBO-side record can be traced back later."""
    __tablename__ = "quickbooks_journal_sync_logs"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    journal_entry_id: str = Field(index=True, unique=True)
    qb_journal_entry_id: str
    synced_at: datetime = Field(default_factory=datetime.utcnow)
    synced_by: str


class QuickBooksJournalPushResult(SQLModel):
    pushed: int
    skipped: int
    errors: List[str] = []


class BiometricDevicePurpose(str, Enum):
    CANTEEN = "canteen"
    STAFF_ATTENDANCE = "staff_attendance"
    STUDENT_ATTENDANCE = "student_attendance"


class BiometricDevice(SQLModel, table=True):
    """A registered fingerprint/face-scan device (or the vendor middleware
    fronting one — e.g. a ZKTeco push gateway) allowed to post attendance
    punches for this school. Registration is a prerequisite for punches:
    routers/public_api.py's biometric punch endpoints look a device up by
    (school_id, device_serial) and reject anything not registered/active,
    so a stolen API key alone isn't enough to inject attendance — the
    device also has to be one the school explicitly added here.

    `purpose` also gates routers/biometric_adms.py's raw ADMS push
    receiver (for devices with no vendor middleware in between) — it's how
    that single receiver knows whether an incoming PIN should resolve
    against Student.biometric_device_pin for a canteen wallet event
    (CANTEEN), Staff.biometric_device_pin for a staff punch
    (STAFF_ATTENDANCE), or Student.biometric_device_pin again for a gate
    check-in/out (STUDENT_ATTENDANCE) — canteen and student-attendance
    devices share the same student PIN field, differentiated only by which
    device the scan came in on."""
    __tablename__ = "biometric_devices"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    device_serial: str = Field(index=True)  # the identifier the device/middleware sends on every punch
    name: str
    location: Optional[str] = None
    purpose: BiometricDevicePurpose = Field(default=BiometricDevicePurpose.STAFF_ATTENDANCE)
    is_active: bool = Field(default=True, index=True)
    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_seen_at: Optional[datetime] = None
    # Set by services.scheduler's device-health sweep the first time this
    # device is found stale (no ping past the threshold); reset to None the
    # next time it actually reports in (routers/biometric_adms.py) — so a
    # device that goes quiet gets exactly one alert per outage, not one
    # every sweep, but a NEW outage after it recovers alerts again.
    stale_alerted_at: Optional[datetime] = None


class BiometricRejectedScan(SQLModel, table=True):
    """A biometric PIN that came in on a REGISTERED, active device but
    matched no staff/student — previously this only ever went to a log
    line (routers/biometric_adms.py), invisible unless someone tailed
    application logs. A repeated mismatch (e.g. a new student not yet
    PIN-enrolled scanning every morning with zero result) is now visible
    on a school's own admin screen. Deliberately does NOT cover a push
    from an unregistered device/serial — that case has no school_id to
    attribute the row to, so it stays log-only."""
    __tablename__ = "biometric_rejected_scans"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    device_serial: str = Field(index=True)
    purpose: BiometricDevicePurpose
    pin: str
    reason: str  # "no_matching_staff" | "no_matching_student"
    created_at: datetime = Field(default_factory=datetime.utcnow)


class BiometricDeviceCreate(SQLModel):
    device_serial: str
    name: str
    location: Optional[str] = None
    purpose: BiometricDevicePurpose


class BiometricDeviceUpdate(SQLModel):
    name: Optional[str] = None
    location: Optional[str] = None
    purpose: Optional[BiometricDevicePurpose] = None
    is_active: Optional[bool] = None


class BiometricDeviceResponse(SQLModel):
    id: str
    device_serial: str
    name: str
    location: Optional[str]
    purpose: BiometricDevicePurpose
    is_active: bool
    created_at: datetime
    last_seen_at: Optional[datetime]
    # Computed by the list endpoint (routers/integrations.py), not stored —
    # a device is stale if it's never reported in past a grace period, or
    # hasn't reported in recently. See services.scheduler's device-health
    # sweep for the same threshold used to proactively alert on this.
    is_stale: bool = False


class BiometricStudentPunch(SQLModel):
    device_serial: str
    student_id: str  # Student.student_id — the school's human-readable ID, not the internal UUID
    punched_at: Optional[datetime] = None  # device's own clock; defaults to server time if omitted


class BiometricStaffPunch(SQLModel):
    device_serial: str
    staff_id: str  # Staff.staff_id — the school's human-readable ID, not the internal UUID
    punched_at: Optional[datetime] = None
    # "check_in" / "check_out", or omitted for auto-toggle (no check-in yet
    # today -> check-in; check-in but no check-out -> check-out).
    event_type: Optional[str] = None


class LmsSyncRecord(SQLModel, table=True):
    """Idempotency map for LMS-pushed assignments: an external LMS's own id
    for an assignment it pushed -> the internal Assignment row it became.
    Lets an LMS re-sync the same assignment on every sync run (its normal
    behavior) without creating a duplicate each time — routers/public_api.py's
    POST /lms/assignments looks this up by (school_id, external_id) and
    updates the existing row when found. Submissions don't need this: an
    LMS submission is naturally deduplicated by (assignment_id, student_id),
    an already-unique pair, with no synthetic external id required."""
    __tablename__ = "lms_sync_records"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    external_id: str = Field(index=True)  # the LMS's own id for the assignment
    internal_id: str  # Assignment.id
    created_at: datetime = Field(default_factory=datetime.utcnow)


class LmsAssignmentPush(SQLModel):
    external_id: Optional[str] = None  # the LMS's own id for this assignment; omit to always create new
    class_id: str
    subject_id: str
    staff_id: str  # Staff.staff_id — the teacher of record
    academic_term_id: Optional[str] = None  # defaults to the school's current term if omitted
    title: str
    description: str
    assignment_type: AssignmentType = AssignmentType.HOMEWORK
    points_possible: float = 100.0
    due_date: Optional[datetime] = None


class LmsSubmissionPush(SQLModel):
    assignment_id: str  # the id returned from POST /lms/assignments
    student_id: str  # Student.student_id — the school's human-readable ID
    status: SubmissionStatus = SubmissionStatus.SUBMITTED
    score: Optional[float] = None
    feedback: Optional[str] = None
    submitted_at: Optional[datetime] = None
