"""Discipline / Incident Tracking Models"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey
from typing import List, Optional
from datetime import datetime
from enum import Enum
import uuid


class IncidentSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class DisciplineActionType(str, Enum):
    WARNING = "warning"
    DETENTION = "detention"
    SUSPENSION = "suspension"
    PARENT_MEETING = "parent_meeting"
    OTHER = "other"


class ActionApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# A suspension created directly by a TEACHER requires SCHOOL_ADMIN/SUPER_ADMIN
# sign-off before it counts — see routers/discipline.py::create_incident_action.
# Not currently used for auto-flagging at a tally threshold (lean scope: the
# approval gate itself is the escalation control), but named for that intent.
SUSPENSION_DEMERIT_THRESHOLD = 10


class IncidentReport(SQLModel, table=True):
    """A single reported disciplinary incident, possibly involving multiple students"""
    __tablename__ = "incident_reports"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)

    reporter_staff_id: str = Field(index=True)
    incident_date: str
    category: str
    description: str
    severity: IncidentSeverity

    academic_term_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("academic_terms.id", ondelete="SET NULL"), index=True)
    )

    status: str = "open"  # open, resolved

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class IncidentReportCreate(SQLModel):
    incident_date: str
    category: str
    description: str
    severity: IncidentSeverity
    academic_term_id: Optional[str] = None
    student_ids: List[str]


class IncidentReportUpdate(SQLModel):
    incident_date: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    severity: Optional[IncidentSeverity] = None
    status: Optional[str] = None


class IncidentStudent(SQLModel, table=True):
    """Join table linking an incident to the student(s) involved"""
    __tablename__ = "incident_students"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    incident_id: str = Field(sa_column=Column(String, ForeignKey("incident_reports.id", ondelete="CASCADE"), index=True))
    student_id: str = Field(index=True)
    role_in_incident: Optional[str] = None  # e.g. "primary", "witness"


class IncidentAction(SQLModel, table=True):
    """A disciplinary action taken against a specific student for an incident"""
    __tablename__ = "incident_actions"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    incident_id: str = Field(sa_column=Column(String, ForeignKey("incident_reports.id", ondelete="CASCADE"), index=True))
    student_id: str = Field(index=True)

    action_type: DisciplineActionType
    action_date: str
    details: Optional[str] = None
    demerit_points: int = 0

    # Who created this specific sanction (distinct from IncidentReport.
    # reporter_staff_id, which names who reported the underlying incident) —
    # needed so approve/reject can block the author from signing off on
    # their own action. Nullable since actions created before this column
    # existed have no recorded author.
    recorded_by: Optional[str] = None

    # A SUSPENSION always starts PENDING and must be approved by a DIFFERENT
    # SCHOOL_ADMIN/SUPER_ADMIN before it counts toward the demerit tally —
    # including one created by an admin, not just a TEACHER (maker-checker:
    # the person who sanctions a student shouldn't also be the sole sign-off
    # on suspending their exam eligibility). Every other action type is
    # APPROVED immediately — see routers/discipline.py::create_incident_action.
    approval_status: ActionApprovalStatus = ActionApprovalStatus.APPROVED
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None

    # A SUSPENSION action has no built-in duration (just action_date, a
    # single point in time) — an APPROVED suspension is treated as an
    # ongoing exam-eligibility restriction (see routers/exams.py::
    # generate_seating) until a staff member explicitly clears it via
    # /discipline/actions/{id}/clear.
    cleared: bool = False
    cleared_by: Optional[str] = None
    cleared_at: Optional[datetime] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)


class IncidentActionCreate(SQLModel):
    student_id: str
    action_type: DisciplineActionType
    action_date: str
    details: Optional[str] = None
    demerit_points: int = 0


class RejectActionRequest(SQLModel):
    rejection_reason: Optional[str] = None
