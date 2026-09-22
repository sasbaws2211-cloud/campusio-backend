"""Learning Analytics Models for student performance insights"""
from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime
from enum import Enum


class RiskLevel(str, Enum):
    """Student performance risk classification"""
    LOW = "low"              # On track, good performance
    MODERATE = "moderate"    # Some concerns, monitor
    HIGH = "high"            # Significant risk, intervention needed
    CRITICAL = "critical"    # Failing, urgent action required


class AnalyticsSnapshot(SQLModel, table=True):
    """
    Periodic student analytics snapshot for historical trending.
    Captured at end of term or on-demand for performance analysis.
    """
    __tablename__ = "analytics_snapshots"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    school_id: str = Field(foreign_key="schools.id", index=True)
    student_id: str = Field(foreign_key="students.id", index=True)
    academic_term_id: str = Field(foreign_key="academic_terms.id", index=True)
    
    # Performance metrics
    overall_gpa: Optional[float] = Field(default=None)  # Grade point average (1-9)
    attendance_rate: Optional[float] = Field(default=None)  # 0-100%
    assignment_completion_rate: Optional[float] = Field(default=None)  # 0-100%
    
    # Subject-level performance (best/worst subject)
    best_subject: Optional[str] = None
    best_subject_grade: Optional[int] = None
    worst_subject: Optional[str] = None
    worst_subject_grade: Optional[int] = None
    
    # Trend indicators
    gpa_trend: Optional[str] = None  # "improving", "stable", "declining"
    attendance_trend: Optional[str] = None  # "improving", "stable", "declining"
    
    # Risk assessment — plain str (validated by RiskLevel only at the
    # service/API layer). The actual DB column (migration f8a2b6d4c9e5) is a
    # plain VARCHAR, but this field used to be typed as the raw RiskLevel
    # Enum class, which makes SQLAlchemy assume a native Postgres enum type
    # exists and cast every bind param to ::risklevel — a type Postgres
    # never actually created here, so EVERY query touching this column
    # (any ==, in_, or ORDER BY) failed with "operator does not exist:
    # character varying = risklevel". See models/parent_requests.py for why
    # this codebase avoids Enum-typed columns on every table built since.
    risk_level: str = Field(default=RiskLevel.LOW.value)
    risk_factors: Optional[str] = None  # JSON-encoded list of flags (e.g., ["low_gpa", "poor_attendance"])
    
    captured_at: datetime = Field(default_factory=datetime.utcnow)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ClassPerformanceSummary(SQLModel, table=True):
    """
    Class-level aggregate analytics for teacher/admin dashboards.
    Refreshed periodically (daily or on-demand).
    """
    __tablename__ = "class_performance_summaries"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    school_id: str = Field(foreign_key="schools.id", index=True)
    class_id: str = Field(foreign_key="classes.id", index=True)
    academic_term_id: str = Field(foreign_key="academic_terms.id", index=True)
    
    # Aggregate metrics
    total_students: int = 0
    average_gpa: Optional[float] = None
    average_attendance_rate: Optional[float] = None
    
    # Performance distribution
    students_at_risk: int = 0  # HIGH + CRITICAL risk level
    students_passing: int = 0  # Grade 1-6
    students_failing: int = 0  # Grade 7-9
    
    # Subject insights
    strongest_subject: Optional[str] = None  # Subject with highest average grade
    weakest_subject: Optional[str] = None    # Subject with lowest average grade
    
    # Trends
    gpa_trend: Optional[str] = None  # "improving", "stable", "declining" vs previous term
    at_risk_trend: Optional[str] = None  # "more_at_risk" vs previous term, etc.
    
    last_updated: datetime = Field(default_factory=datetime.utcnow)
    created_at: datetime = Field(default_factory=datetime.utcnow)
