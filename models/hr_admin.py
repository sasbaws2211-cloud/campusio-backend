"""Benefits, staff conduct, exits, and workforce planning records."""
from datetime import datetime
from enum import Enum
from typing import Optional
import uuid

from sqlmodel import Field, SQLModel
from sqlalchemy import Column, String, ForeignKey


class BenefitPlan(SQLModel, table=True):
    """The catalog a school chooses from — StaffBenefit rows below are one
    staff member's enrollment, optionally against one of these (plan_id is
    nullable so an ad-hoc StaffBenefit not tied to any catalog entry, the
    only kind that existed before this model, keeps working unchanged)."""
    __tablename__ = "hr_benefit_plans"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    name: str
    benefit_type: str
    provider: Optional[str] = None
    description: Optional[str] = None
    cost_to_school: Optional[float] = None
    cost_to_staff: Optional[float] = None
    is_active: bool = Field(default=True, index=True)
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class BenefitPlanCreate(SQLModel):
    name: str
    benefit_type: str
    provider: Optional[str] = None
    description: Optional[str] = None
    cost_to_school: Optional[float] = None
    cost_to_staff: Optional[float] = None


class BenefitPlanUpdate(SQLModel):
    name: Optional[str] = None
    description: Optional[str] = None
    cost_to_school: Optional[float] = None
    cost_to_staff: Optional[float] = None
    is_active: Optional[bool] = None


class StaffBenefit(SQLModel, table=True):
    __tablename__ = "hr_staff_benefits"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    staff_id: str = Field(sa_column=Column(String, ForeignKey("staff.id", ondelete="CASCADE"), index=True))
    plan_id: Optional[str] = Field(default=None, index=True)  # BenefitPlan.id — None for an ad-hoc benefit not from the catalog
    benefit_type: str
    provider: Optional[str] = None
    coverage_amount: Optional[float] = None
    start_date: str
    end_date: Optional[str] = None
    status: str = "active"
    notes: Optional[str] = None
    recorded_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class StaffBenefitCreate(SQLModel):
    staff_id: str
    plan_id: Optional[str] = None
    benefit_type: str
    provider: Optional[str] = None
    coverage_amount: Optional[float] = None
    start_date: str
    end_date: Optional[str] = None
    status: str = "active"
    notes: Optional[str] = None


class StaffDisciplinaryAction(SQLModel, table=True):
    """action_type is a free string, not a fixed enum — deliberately, so
    each school can define its own warning ladder (e.g. "Verbal Warning",
    "First Written Warning", "Final Written Warning", "Suspension") rather
    than being forced into one this codebase hardcodes. status starts
    "open" and is expected to move to "resolved" (via the update endpoint,
    which stamps resolved_at) or "escalated"/"appealed" as a case
    progresses — previously nothing could ever change it once created."""
    __tablename__ = "hr_staff_disciplinary_actions"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    staff_id: str = Field(sa_column=Column(String, ForeignKey("staff.id", ondelete="CASCADE"), index=True))
    incident_date: str
    action_type: str
    description: str
    outcome: Optional[str] = None
    status: str = "open"
    resolved_at: Optional[datetime] = None
    recorded_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # The affected staff member confirming they've seen this record — the
    # same "acknowledgment is a separate signal from HR closing the case"
    # split already used for performance reviews (see routers/hr.py's
    # acknowledge_review). Previously this model had no acknowledgment
    # field at all, and no endpoint let the staff member even view it.
    acknowledged_at: Optional[datetime] = None


class StaffDisciplinaryActionCreate(SQLModel):
    staff_id: str
    incident_date: str
    action_type: str
    description: str
    outcome: Optional[str] = None
    status: str = "open"


class StaffDisciplinaryActionUpdate(SQLModel):
    action_type: Optional[str] = None
    outcome: Optional[str] = None
    status: Optional[str] = None  # open/resolved/escalated/appealed


class StaffGrievanceCategory(str, Enum):
    HARASSMENT = "harassment"
    UNFAIR_TREATMENT = "unfair_treatment"
    WORKPLACE_SAFETY = "workplace_safety"
    COMPENSATION_DISPUTE = "compensation_dispute"
    WORKLOAD = "workload"
    OTHER = "other"


class StaffGrievanceStatus(str, Enum):
    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class StaffGrievance(SQLModel, table=True):
    """A staff-initiated concern — previously there was genuinely no
    channel anywhere for this: models.complaints.Complaint is explicitly
    parent/student -> school (including for a parent complaining ABOUT a
    staff member, the reverse direction), and StaffDisciplinaryAction above
    is management-initiated only. A separate model rather than reusing
    Complaint: visibility here is deliberately narrow (the submitter and
    HR/admin only — never broadcast the way a parent complaint might
    surface to assigned staff generally), since a grievance is often about
    a colleague or supervisor and mishandled visibility would itself be a
    safeguarding failure."""
    __tablename__ = "hr_staff_grievances"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    submitted_by_staff_id: str = Field(sa_column=Column(String, ForeignKey("staff.id", ondelete="CASCADE"), index=True))
    # Who/what this is about — optional, since a grievance can be about a
    # general workplace issue with no single named person (e.g. workplace
    # safety). No FK enforcement beyond existence-checking at the router
    # layer, consistent with this model's other cross-references.
    against_staff_id: Optional[str] = Field(default=None, index=True)
    category: str = StaffGrievanceCategory.OTHER.value
    subject: str
    description: str
    status: str = StaffGrievanceStatus.SUBMITTED.value
    assigned_to: Optional[str] = Field(default=None, index=True)  # User.id of the HR staff handling it
    resolution_notes: Optional[str] = None
    resolved_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class StaffGrievanceCreate(SQLModel):
    against_staff_id: Optional[str] = None
    category: StaffGrievanceCategory = StaffGrievanceCategory.OTHER
    subject: str
    description: str


class StaffGrievanceUpdate(SQLModel):
    status: Optional[StaffGrievanceStatus] = None
    assigned_to: Optional[str] = None
    resolution_notes: Optional[str] = None


class StaffExit(SQLModel, table=True):
    __tablename__ = "hr_staff_exits"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    staff_id: str = Field(sa_column=Column(String, ForeignKey("staff.id", ondelete="CASCADE"), index=True))
    exit_type: str
    notice_date: Optional[str] = None
    last_working_date: str
    reason: Optional[str] = None
    clearance_status: str = "pending"
    assets_cleared: bool = False
    finance_cleared: bool = False
    hr_cleared: bool = False
    final_settlement_amount: Optional[float] = None
    settled_at: Optional[datetime] = None
    notes: Optional[str] = None
    recorded_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # Exit interview — separate from clearance (clearance is asset/finance/HR
    # sign-off; the interview is a retention/culture signal, so it's tracked
    # independently and doesn't gate clearance either way).
    exit_interview_completed: bool = False
    exit_interview_date: Optional[str] = None
    exit_interview_notes: Optional[str] = None
    would_recommend_employer: Optional[bool] = None
    primary_reason_category: Optional[str] = None  # voluntary/involuntary/retirement/end_of_contract


class StaffExitCreate(SQLModel):
    staff_id: str
    exit_type: str
    notice_date: Optional[str] = None
    last_working_date: str
    reason: Optional[str] = None
    final_settlement_amount: Optional[float] = None
    notes: Optional[str] = None
    primary_reason_category: Optional[str] = None


class StaffExitUpdate(SQLModel):
    clearance_status: Optional[str] = None
    assets_cleared: Optional[bool] = None
    finance_cleared: Optional[bool] = None
    hr_cleared: Optional[bool] = None
    final_settlement_amount: Optional[float] = None
    notes: Optional[str] = None
    # When finance_cleared=True is requested in the SAME call and this
    # staff member still has outstanding loan balance(s), pay those loans
    # off in full right now (see StaffLoanService.settle_loan_at_exit)
    # instead of blocking the clearance — previously the only options were
    # manually recovering the balance via payroll installments (slow, and
    # this staff member is leaving) or writing it off entirely (forgiving a
    # debt that may be perfectly recoverable from a final settlement).
    settle_loans_from_final_pay: Optional[bool] = None


class StaffExitInterview(SQLModel):
    exit_interview_date: str
    exit_interview_notes: Optional[str] = None
    would_recommend_employer: Optional[bool] = None
    primary_reason_category: Optional[str] = None


class WorkforcePlan(SQLModel, table=True):
    __tablename__ = "hr_workforce_plans"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    period: str
    department: str
    planned_positions: int
    current_positions: int = 0
    budget_amount: Optional[float] = None
    notes: Optional[str] = None
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class WorkforcePlanCreate(SQLModel):
    period: str
    department: str
    planned_positions: int
    current_positions: int = 0
    budget_amount: Optional[float] = None
    notes: Optional[str] = None