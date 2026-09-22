"""Grade and Report Card models"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey
from typing import Optional, List
from datetime import datetime
from enum import Enum
import uuid


class AssessmentType(str, Enum):
    CLASS_WORK = "class_work"
    HOMEWORK = "homework"
    QUIZ = "quiz"
    MID_TERM = "mid_term"
    END_OF_TERM = "end_of_term"
    PROJECT = "project"


class Grade(SQLModel, table=True):
    __tablename__ = "grades"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)
    class_id: str = Field(index=True)
    subject_id: str = Field(index=True)
    academic_term_id: str = Field(sa_column=Column(String, ForeignKey("academic_terms.id", ondelete="CASCADE"), index=True))
    assessment_type: AssessmentType
    score: float
    max_score: float
    weight: float = 1.0
    remarks: Optional[str] = None
    recorded_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class GradeCreate(SQLModel):
    student_id: str
    class_id: str
    subject_id: str
    academic_term_id: str
    assessment_type: AssessmentType
    score: float
    max_score: float
    weight: float = 1.0
    remarks: Optional[str] = None


class GradingScheme(SQLModel, table=True):
    """A named, school-configured grading scale (e.g. "JHS Grading Scale",
    "Primary Grading Scale", "Sciences Grading Scale"). class_level/subject_id
    scope which classes/subjects it applies to — either left None to mean
    "every level"/"every subject" respectively. See services/grading_service.py
    for how a scheme is picked for a given class level + subject at grading
    time (most-specific match wins), and utils/grade_scale.py's GES_GRADE_SCALE
    for the built-in default used by any school that hasn't configured one.

    ca_weight/exam_weight are the same kind of school-configurable policy as
    the grade bands below, just for the OTHER half of GES report-card
    computation: how much of the 100-point total comes from continuous
    assessment (SBA: classwork/homework/quiz/mid-term/project) vs. the
    end-of-term exam. Default 50/50 matches the previous hardcoded behavior
    in services/report_card_pdf_service.py exactly, so a school that hasn't
    configured a scheme keeps grading exactly as before. Always sum to 100
    — enforced by CreateGradingSchemeRequest/UpdateGradingSchemeRequest
    validation, not a DB constraint (SQLite/dev flexibility, same convention
    as the rest of this model)."""
    __tablename__ = "grading_schemes"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    name: str
    class_level: Optional[str] = Field(default=None, index=True)  # models.classroom.ClassLevel value, or None = every level
    subject_id: Optional[str] = Field(default=None, index=True)   # None = every subject
    ca_weight: float = 50.0
    exam_weight: float = 50.0
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class GradeBandInput(SQLModel):
    grade: str
    min_score: float
    max_score: float
    description: str
    gpa_point: float


class CreateGradingSchemeRequest(SQLModel):
    name: str
    class_level: Optional[str] = None
    subject_id: Optional[str] = None
    ca_weight: float = 50.0
    exam_weight: float = 50.0
    bands: List[GradeBandInput]


class UpdateGradingSchemeRequest(SQLModel):
    name: Optional[str] = None
    class_level: Optional[str] = None
    subject_id: Optional[str] = None
    ca_weight: Optional[float] = None
    exam_weight: Optional[float] = None
    is_active: Optional[bool] = None
    bands: Optional[List[GradeBandInput]] = None  # when provided, replaces every existing band


class GradeScale(SQLModel, table=True):
    __tablename__ = "grade_scales"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    # None = a legacy/ungrouped row from the old POST /grades/scales endpoint —
    # never consulted at grading time, kept only so pre-existing rows aren't
    # orphaned. Every band that actually affects a computed grade belongs to
    # a GradingScheme.
    scheme_id: Optional[str] = Field(default=None, index=True)
    grade: str
    min_score: float
    max_score: float
    description: str
    gpa_point: float


class ReportCardStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"


class PromotionDecision(str, Enum):
    PROMOTED = "promoted"
    REPEATED = "repeated"
    GRADUATED = "graduated"


class ReportCard(SQLModel, table=True):
    __tablename__ = "report_cards"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)
    class_id: str = Field(index=True)
    academic_term_id: str = Field(sa_column=Column(String, ForeignKey("academic_terms.id", ondelete="CASCADE"), index=True))
    total_score: float
    average_score: float
    position: Optional[int] = None
    class_size: int
    attendance_percentage: float
    class_teacher_remarks: Optional[str] = None
    head_teacher_remarks: Optional[str] = None
    conduct: Optional[str] = None
    interest: Optional[str] = None
    # GES SBA terminal-report fields
    attitude: Optional[str] = None
    days_present: Optional[int] = None   # Attendance: days present out of...
    days_total: Optional[int] = None     # ...total school days in the term
    vacation_date: Optional[str] = None    # e.g. "12th December, 2026"
    reopening_date: Optional[str] = None   # e.g. "6th January, 2027"
    promoted_to: Optional[str] = None      # Term 3 only, e.g. "Primary 5" — free-text label printed on the PDF
    # Structured counterpart of promoted_to, actually consumed by year rollover
    # (routers/academic_calendar.py) to decide where a student lands next year.
    # None = rollover treats it as "promoted" by default (see rollover docstring).
    promotion_decision: Optional[str] = None
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    generated_by: str
    # Sign-off: a report card is generated (usually by the class teacher) in
    # DRAFT and stays invisible to parents/students until a school admin
    # (standing in for the head teacher — there's no separate head_teacher
    # role in this system) approves it. Re-generating an already-approved
    # card (e.g. a grade correction) resets it back to DRAFT so a stale
    # version never ships without a fresh sign-off.
    status: ReportCardStatus = ReportCardStatus.DRAFT
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None


class ReportCardRecall(SQLModel, table=True):
    """One row per 'call back this report card for correction' action
    (routers/grades.py's recall_report_card endpoint) — an explicit,
    reason-required admin action distinct from the implicit DRAFT reversion
    that already happens inside generate_report_card. Snapshots the report
    card's key figures at the moment of recall (a lightweight record of what
    was being corrected, not just that a correction happened), and gets
    stamped with resent_at/resent_by once the corrected version is
    re-approved (see approve_report_card) so the admin UI can show a real
    recall -> correct -> resend history per student/term."""
    __tablename__ = "report_card_recalls"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    report_card_id: str = Field(index=True)
    student_id: str = Field(index=True)
    academic_term_id: str = Field(index=True)
    reason: str
    recalled_by: str
    recalled_at: datetime = Field(default_factory=datetime.utcnow)
    snapshot_total_score: float
    snapshot_average_score: float
    snapshot_class_teacher_remarks: Optional[str] = None
    snapshot_head_teacher_remarks: Optional[str] = None
    resent_at: Optional[datetime] = None
    resent_by: Optional[str] = None


class RecallReportCardRequest(SQLModel):
    reason: str


class StandardMasteryRecord(SQLModel, table=True):
    """Optional, parallel standards-based-grading record — purely additive
    alongside the numeric Grade/percentage ReportCard model above, never a
    replacement for it. standard_id references models.curriculum.CurriculumStandard
    but is deliberately not a hard DB ForeignKey (cross-module reference,
    app-layer validated only — same convention this codebase already uses for
    other cross-domain references, e.g. FacilityAsset.inventory_asset_id)."""
    __tablename__ = "standard_mastery_records"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)
    standard_id: str = Field(index=True)
    grade_id: Optional[str] = Field(default=None, index=True)
    class_id: str = Field(index=True)
    subject_id: str = Field(index=True)
    academic_term_id: str = Field(index=True)
    mastery_level: str  # "not_yet" | "approaching" | "meets" | "exceeds" — plain str
    assessed_by: str
    assessed_at: datetime = Field(default_factory=datetime.utcnow)
    notes: Optional[str] = None


class StandardMasteryRecordCreate(SQLModel):
    student_id: str
    standard_id: str
    grade_id: Optional[str] = None
    class_id: str
    subject_id: str
    academic_term_id: str
    mastery_level: str
    notes: Optional[str] = None


MASTERY_LEVELS = ("not_yet", "approaching", "meets", "exceeds")
