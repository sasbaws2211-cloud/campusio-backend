"""Student and Parent models"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey, JSON
from typing import Optional
from datetime import datetime
from enum import Enum
import uuid


class Gender(str, Enum):
    MALE = "male"
    FEMALE = "female"


class StudentStatus(str, Enum):
    ACTIVE = "active"
    GRADUATED = "graduated"
    TRANSFERRED = "transferred"
    WITHDRAWN = "withdrawn"
    EXPELLED = "expelled"
    # Temporary, unlike the four states above — a student on a leave of
    # absence/medical leave/disciplinary suspension who is expected back.
    # Does not go through the exit-clearance flow; see deactivate_student/
    # reactivate_student in routers/students.py.
    INACTIVE = "inactive"


class Student(SQLModel, table=True):
    __tablename__ = "students"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)  # School-specific ID
    first_name: str
    last_name: str
    other_names: Optional[str] = None
    date_of_birth: str
    gender: Gender
    admission_date: str
    class_id: Optional[str] = Field(default=None, index=True)
    campus_id: Optional[str] = Field(default=None, index=True)
    address: Optional[str] = None
    nationality: str = "Ghanaian"
    religion: Optional[str] = None
    blood_group: Optional[str] = None
    medical_conditions: Optional[str] = None
    photo_url: Optional[str] = None
    status: StudentStatus = StudentStatus.ACTIVE
    user_id: Optional[str] = Field(default=None, index=True)

    # Hashed 4-digit PIN for kiosk (shared-device) portal login — see
    # routers/kiosk.py. NULL means kiosk login isn't set up for this
    # student yet (set/reset by a parent or staff).
    kiosk_pin_hash: Optional[str] = None

    # The numeric PIN this student was assigned on a school's biometric
    # (face-recognition) terminal at local enrollment time — see
    # routers/biometric_adms.py. Not a secret (unlike kiosk_pin_hash): it's
    # just the join key between a device's recognition event and this
    # student, plaintext by design. NULL means not enrolled on any terminal.
    biometric_device_pin: Optional[str] = Field(default=None, index=True)

    # Exit — populated only via the dedicated exit workflow (routers/students.py
    # exit_student), never by the generic update endpoint.
    exit_date: Optional[str] = None
    exit_reason: Optional[str] = None
    transfer_destination_school: Optional[str] = None

    # Transfer-in — captured at admission time when this student is joining
    # from another school rather than enrolling fresh.
    admission_type: str = "new"  # "new" | "transfer_in"
    previous_school_name: Optional[str] = None
    transfer_certificate_number: Optional[str] = None

    # Freeform school-defined fields (e.g. bus route, uniform size) that
    # don't warrant a dedicated column — mirrors models/integrations.py's
    # ApiKey.scopes sa_type=JSON pattern.
    custom_fields: Optional[dict] = Field(default=None, sa_type=JSON)

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class StudentCreate(SQLModel):
    student_id: Optional[str] = None
    first_name: str
    last_name: str
    other_names: Optional[str] = None
    date_of_birth: str
    gender: Gender
    admission_date: str
    class_id: Optional[str] = None
    campus_id: Optional[str] = None
    address: Optional[str] = None
    nationality: str = "Ghanaian"
    religion: Optional[str] = None
    blood_group: Optional[str] = None
    medical_conditions: Optional[str] = None
    photo_url: Optional[str] = None
    status: StudentStatus = StudentStatus.ACTIVE
    admission_type: str = "new"
    previous_school_name: Optional[str] = None
    transfer_certificate_number: Optional[str] = None
    custom_fields: Optional[dict] = None


class StudentExitRequest(SQLModel):
    status: StudentStatus
    reason: str
    exit_date: Optional[str] = None
    transfer_destination_school: Optional[str] = None
    confirm_incomplete_clearance: bool = False


class StudentDeactivateRequest(SQLModel):
    reason: str


class StudentReEnrollRequest(SQLModel):
    """Bring a formerly-graduated/transferred/withdrawn/expelled student back
    to ACTIVE status in a (possibly new) class — distinct from /reactivate,
    which only ever reverses the temporary INACTIVE status."""
    class_id: str
    reason: str


class StudentStatusEvent(SQLModel, table=True):
    """Audit trail for Student.status transitions — the student-side
    equivalent of ApplicantStageEvent in models/admissions_enterprise.py."""
    __tablename__ = "student_status_events"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(sa_column=Column(String, ForeignKey("students.id", ondelete="CASCADE"), index=True))
    from_status: str
    to_status: str
    reason: Optional[str] = None
    changed_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Parent(SQLModel, table=True):
    __tablename__ = "parents"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    first_name: str
    last_name: str
    relationship: str
    phone: str
    email: Optional[str] = None
    occupation: Optional[str] = None
    address: Optional[str] = None
    is_emergency_contact: bool = False
    user_id: Optional[str] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class CustodyType(str, Enum):
    """Validated at the API layer only (ParentCreate.custody_type /
    ParentCustodyUpdate.custody_type) — StudentParent.custody_type is a
    plain string column, deliberately NOT typed as this Enum, so it never
    becomes a native Postgres enum requiring its own ALTER TYPE migration
    for every future custody type added. Same convention/reasoning as
    models/admissions.py's RejectionReasonCode."""
    LEGAL_GUARDIAN = "legal_guardian"
    CUSTODIAL = "custodial"
    NON_CUSTODIAL = "non_custodial"
    EMERGENCY_ONLY = "emergency_only"


class ParentCreate(SQLModel):
    first_name: str
    last_name: str
    relationship: str
    phone: str
    email: Optional[str] = None
    occupation: Optional[str] = None
    address: Optional[str] = None
    is_emergency_contact: bool = False
    # Per-(student,parent)-pair custody, not per-parent — see StudentParent.
    # Not part of the Parent table itself; routers/students.py pulls this
    # out before constructing the Parent row and stamps it on the link.
    custody_type: Optional[str] = None


class ParentCustodyUpdate(SQLModel):
    custody_type: str


class StudentParent(SQLModel, table=True):
    __tablename__ = "student_parents"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    student_id: str = Field(index=True)
    parent_id: str = Field(index=True)
    # legal_guardian | custodial | non_custodial | emergency_only — validated
    # at the router layer against CustodyType above, not DB-enforced (see
    # native-enum-avoidance convention). Per-pair, since e.g. a grandmother
    # could be legal guardian for one grandchild and just an emergency
    # contact for another.
    custody_type: str = "guardian"
    # Court-order enforcement — deliberately separate from custody_type
    # above: "non_custodial" does NOT by itself mean a parent has no pickup
    # rights (many jurisdictions grant visitation/pickup rights to a
    # non-custodial parent), so this is never auto-derived from
    # custody_type. Only an admin, acting on an actual court order, sets
    # this explicitly. When set, routers/security.py hard-denies this
    # parent's pickup-related actions (QR issuance, "on my way", sharing
    # the pickup QR) — see assert_parent_pickup_allowed.
    is_pickup_restricted: bool = False
    restriction_reason: Optional[str] = None
    restricted_by: Optional[str] = None
    restricted_at: Optional[datetime] = None


class StudentEnrollment(SQLModel, table=True):
    """One row per (student, class, term) stretch — a year-scoped roster
    history distinct from Student.class_id, which only ever holds where a
    student is *right now*. Grade/Attendance/ReportCard already stamp their
    own class_id + academic_term_id at time of recording, so those stay
    accurate regardless of later moves; this table exists so "who was in
    Class 4 during 2024/2025" can be answered directly instead of inferred
    from grade or attendance rows. ended_at/ended_reason close a row out when
    a student is promoted, reassigned, or exits the school; a null ended_at
    means this is the student's current placement."""
    __tablename__ = "student_enrollments"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)
    class_id: str = Field(index=True)
    academic_term_id: str = Field(sa_column=Column(String, ForeignKey("academic_terms.id", ondelete="CASCADE"), index=True))
    enrolled_at: datetime = Field(default_factory=datetime.utcnow)
    ended_at: Optional[datetime] = None
    ended_reason: Optional[str] = None  # e.g. "promoted", "reassigned", "graduated", "transferred", "withdrawn"
    created_at: datetime = Field(default_factory=datetime.utcnow)


class StudentSibling(SQLModel, table=True):
    """Explicit sibling link for step/half-siblings who don't share a
    registered Parent record. Siblings who DO share a parent are derived
    implicitly by joining StudentParent on parent_id — see
    GET /students/{student_id}/siblings in routers/students.py, which
    merges both sources into one deduped, tagged list."""
    __tablename__ = "student_siblings"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)
    sibling_student_id: str = Field(index=True)
    relationship_type: str = "sibling"  # sibling | step | half | twin
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class StudentSiblingCreate(SQLModel):
    sibling_student_id: str
    relationship_type: str = "sibling"


class EmergencyContact(SQLModel, table=True):
    """A dedicated emergency-contact record for a student — distinct from
    Parent.is_emergency_contact, since an emergency contact (a neighbor, a
    family friend) may not be a registered parent/guardian at all."""
    __tablename__ = "emergency_contacts"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(sa_column=Column(String, ForeignKey("students.id", ondelete="CASCADE"), index=True))
    name: str
    relationship: str
    phone: str
    alternate_phone: Optional[str] = None
    priority_order: int = 1
    created_at: datetime = Field(default_factory=datetime.utcnow)


class EmergencyContactCreate(SQLModel):
    name: str
    relationship: str
    phone: str
    alternate_phone: Optional[str] = None
    priority_order: int = 1


class EmergencyContactUpdate(SQLModel):
    name: Optional[str] = None
    relationship: Optional[str] = None
    phone: Optional[str] = None
    alternate_phone: Optional[str] = None
    priority_order: Optional[int] = None


class TransferRequest(SQLModel, table=True):
    """Records-release workflow trail around a student transferring out to
    another school. Deliberately additive/observational: reaching
    status="released" here does NOT auto-call exit_student — Student.status
    is still only ever changed by the existing POST /students/{id}/exit
    endpoint (with status=TRANSFERRED), which staff trigger separately once
    release is confirmed. This table is the audit/workflow trail around that
    decision, not a second mechanism that mutates Student.status."""
    __tablename__ = "transfer_requests"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(sa_column=Column(String, ForeignKey("students.id", ondelete="CASCADE"), index=True))
    requested_by: str
    receiving_school_name: str
    release_date: Optional[str] = None
    documents_released: Optional[str] = None  # comma-separated, matching Document.access_roles's convention
    status: str = "requested"  # requested | records_prepared | released | acknowledged
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class TransferRequestCreate(SQLModel):
    receiving_school_name: str
    release_date: Optional[str] = None
    notes: Optional[str] = None


class TransferRequestUpdate(SQLModel):
    status: Optional[str] = None
    release_date: Optional[str] = None
    documents_released: Optional[str] = None
    notes: Optional[str] = None
