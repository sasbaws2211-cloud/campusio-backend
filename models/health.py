"""Health / Clinic Models"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey
from typing import Optional, List
from datetime import datetime
from enum import Enum
import uuid


class VisitApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class StudentHealthProfile(SQLModel, table=True):
    """A student's standing health record: conditions, allergies, emergency contact"""
    __tablename__ = "student_health_profiles"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(unique=True, index=True)

    blood_group: Optional[str] = None
    allergies: Optional[str] = None
    chronic_conditions: Optional[str] = None
    emergency_medical_contact_name: Optional[str] = None
    emergency_medical_contact_phone: Optional[str] = None

    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class StudentHealthProfileCreate(SQLModel):
    student_id: str
    blood_group: Optional[str] = None
    allergies: Optional[str] = None
    chronic_conditions: Optional[str] = None
    emergency_medical_contact_name: Optional[str] = None
    emergency_medical_contact_phone: Optional[str] = None
    notes: Optional[str] = None


class StudentHealthProfileUpdate(SQLModel):
    blood_group: Optional[str] = None
    allergies: Optional[str] = None
    chronic_conditions: Optional[str] = None
    emergency_medical_contact_name: Optional[str] = None
    emergency_medical_contact_phone: Optional[str] = None
    notes: Optional[str] = None


class ClinicVisit(SQLModel, table=True):
    """A single visit to the school clinic/sick bay"""
    __tablename__ = "clinic_visits"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)

    visit_date: str
    symptoms: str
    treatment_given: str
    referred_out: bool = False
    referred_to: Optional[str] = None
    attended_by: Optional[str] = None

    # User.id of whoever logged the visit (server-set — not accepted from
    # ClinicVisitCreate). Needed for the maker-checker segregation-of-duties
    # check below: the same person who logs a visit cannot also be the one
    # who signs off on it once School.require_maker_checker is enabled.
    created_by: Optional[str] = None

    # Maker-checker (School.require_maker_checker, off by default): when
    # enabled, a visit is logged PENDING and needs sign-off from a different
    # staff member via routers/health.py::approve_clinic_visit. When
    # disabled (the default), visits are auto-approved at creation —
    # identical to this module's original behavior.
    approval_status: VisitApprovalStatus = VisitApprovalStatus.APPROVED
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None

    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ClinicVisitCreate(SQLModel):
    student_id: str
    visit_date: str
    symptoms: str
    treatment_given: str
    referred_out: bool = False
    referred_to: Optional[str] = None
    attended_by: Optional[str] = None
    notes: Optional[str] = None


class ClinicVisitUpdate(SQLModel):
    visit_date: Optional[str] = None
    symptoms: Optional[str] = None
    treatment_given: Optional[str] = None
    referred_out: Optional[bool] = None
    referred_to: Optional[str] = None
    attended_by: Optional[str] = None
    notes: Optional[str] = None


class RejectVisitRequest(SQLModel):
    rejection_reason: Optional[str] = None


class ImmunizationRecord(SQLModel, table=True):
    """A single immunization/vaccination record for a student"""
    __tablename__ = "immunization_records"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)

    vaccine_name: str
    date_administered: str
    due_next_date: Optional[str] = None
    administered_by: Optional[str] = None

    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ImmunizationRecordCreate(SQLModel):
    student_id: str
    vaccine_name: str
    date_administered: str
    due_next_date: Optional[str] = None
    administered_by: Optional[str] = None
    notes: Optional[str] = None


class ImmunizationRecordUpdate(SQLModel):
    vaccine_name: Optional[str] = None
    date_administered: Optional[str] = None
    due_next_date: Optional[str] = None
    administered_by: Optional[str] = None
    notes: Optional[str] = None


# ============================================================================
# MEDICATION ADMINISTRATION RECORD (MAR)
# ============================================================================

class MedicationAdministration(SQLModel, table=True):
    """A single dose administered to a student, tied to the clinic visit it
    was given during. Mirrors ImmunizationRecord's shape (simple child
    record, single parent, basic audit fields) but is parented to a
    ClinicVisit rather than directly to a student."""
    __tablename__ = "medication_administrations"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    clinic_visit_id: str = Field(sa_column=Column(String, ForeignKey("clinic_visits.id", ondelete="CASCADE"), index=True))
    student_id: str = Field(index=True)

    medication_name: str
    dose: str
    route: str  # oral | topical | injection | inhaled
    time_given: datetime
    administered_by: str

    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class MedicationAdministrationCreate(SQLModel):
    medication_name: str
    dose: str
    route: str
    time_given: datetime
    administered_by: str
    notes: Optional[str] = None


class MedicationAdministrationUpdate(SQLModel):
    medication_name: Optional[str] = None
    dose: Optional[str] = None
    route: Optional[str] = None
    time_given: Optional[datetime] = None
    administered_by: Optional[str] = None
    notes: Optional[str] = None


# ============================================================================
# HEALTH INCIDENT REPORTING
# ============================================================================

class HealthIncident(SQLModel, table=True):
    """A reportable health/safety incident involving a student — separate
    from a routine ClinicVisit because it carries severity, witnesses,
    parent-notification tracking, and follow-up state."""
    __tablename__ = "health_incidents"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)
    reporter_staff_id: str = Field(index=True)

    incident_date: str
    incident_type: str  # injury | illness | accident | allergic_reaction
    severity: str = "low"  # low | medium | high | critical
    description: str
    witnesses: Optional[str] = None

    parent_notified: bool = False
    parent_notified_at: Optional[datetime] = None
    follow_up_required: bool = False
    follow_up_notes: Optional[str] = None
    status: str = "open"  # open | resolved

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class HealthIncidentCreate(SQLModel):
    student_id: str
    incident_date: str
    incident_type: str
    severity: str = "low"
    description: str
    witnesses: Optional[str] = None
    parent_notified: bool = False
    follow_up_required: bool = False
    follow_up_notes: Optional[str] = None


class HealthIncidentUpdate(SQLModel):
    incident_date: Optional[str] = None
    incident_type: Optional[str] = None
    severity: Optional[str] = None
    description: Optional[str] = None
    witnesses: Optional[str] = None
    parent_notified: Optional[bool] = None
    parent_notified_at: Optional[datetime] = None
    follow_up_required: Optional[bool] = None
    follow_up_notes: Optional[str] = None
    status: Optional[str] = None


# ============================================================================
# HEALTH SCREENING CAMPAIGNS
# ============================================================================

class HealthScreeningCampaign(SQLModel, table=True):
    """A scheduled screening drive (vision, hearing, dental, BMI, ...)
    targeting a class, with per-student results and a coverage summary."""
    __tablename__ = "health_screening_campaigns"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)

    name: str
    screening_type: str  # vision | hearing | dental | bmi | general
    target_class_id: Optional[str] = Field(default=None, index=True)
    start_date: str
    end_date: Optional[str] = None
    status: str = "planned"  # planned | in_progress | completed | cancelled
    notes: Optional[str] = None
    created_by: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class HealthScreeningCampaignCreate(SQLModel):
    name: str
    screening_type: str
    target_class_id: Optional[str] = None
    start_date: str
    end_date: Optional[str] = None
    notes: Optional[str] = None


class HealthScreeningCampaignUpdate(SQLModel):
    name: Optional[str] = None
    screening_type: Optional[str] = None
    target_class_id: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class HealthScreeningResult(SQLModel, table=True):
    """A single student's result within a screening campaign."""
    __tablename__ = "health_screening_results"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    campaign_id: str = Field(sa_column=Column(String, ForeignKey("health_screening_campaigns.id", ondelete="CASCADE"), index=True))
    student_id: str = Field(index=True)

    result_summary: str
    referred_out: bool = False
    referred_to: Optional[str] = None
    screened_by: Optional[str] = None
    screened_at: datetime = Field(default_factory=datetime.utcnow)
    notes: Optional[str] = None


class HealthScreeningResultCreate(SQLModel):
    student_id: str
    result_summary: str
    referred_out: bool = False
    referred_to: Optional[str] = None
    notes: Optional[str] = None


class HealthScreeningResultBulkCreate(SQLModel):
    results: List[HealthScreeningResultCreate]
