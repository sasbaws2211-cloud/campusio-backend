"""Custom report builder + scheduled/emailed reports.

SavedReportDefinition stores an ad-hoc report shape (source table, filters,
group-by field, aggregation) an admin built once and wants to re-run —
see services/custom_reports_service.py for the constrained-but-real query
engine that executes one of these (or a canned report, for scheduling
purposes) against real data. ScheduledReport attaches a recipient list and
a cadence to either a saved definition or one of the existing canned
reports in services/analytics_reports_service.py, and
services/custom_reports_service.py::run_due_scheduled_reports (wired into
services/scheduler.py) emails the CSV out when due.

Status/category fields are deliberately plain `str` — see
models/parent_requests.py for why this codebase avoids the
native-Postgres-enum trap on new tables.
"""
from datetime import datetime
from enum import Enum
from typing import Optional
import uuid

from sqlmodel import SQLModel, Field


class ReportSource(str, Enum):
    STUDENTS = "students"
    GRADES = "grades"
    ATTENDANCE = "attendance"
    FEES = "fees"


class ReportAggregation(str, Enum):
    COUNT = "count"
    AVG = "avg"
    SUM = "sum"


class SavedReportDefinition(SQLModel, table=True):
    __tablename__ = "saved_report_definitions"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    name: str
    source: str  # ReportSource value
    # JSON-encoded {"class_id": "...", "campus_id": "...", "status": "...",
    # "start_date": "...", "end_date": "..."} — only keys valid for `source`
    # are applied, see services/custom_reports_service.py::FIELD_MAP
    filters: Optional[str] = None
    group_by: str  # a field name from FIELD_MAP[source]["group_by"]
    aggregation: str = ReportAggregation.COUNT.value
    # the numeric field to aggregate when aggregation != count, e.g. "score" for grades
    measure_field: Optional[str] = None
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class SavedReportDefinitionCreate(SQLModel):
    name: str
    source: ReportSource
    filters: Optional[dict] = None
    group_by: str
    aggregation: ReportAggregation = ReportAggregation.COUNT
    measure_field: Optional[str] = None


class SavedReportDefinitionUpdate(SQLModel):
    name: Optional[str] = None
    filters: Optional[dict] = None
    group_by: Optional[str] = None
    aggregation: Optional[ReportAggregation] = None
    measure_field: Optional[str] = None


class AdHocReportRequest(SQLModel):
    """Run a report shape without saving it first."""
    source: ReportSource
    filters: Optional[dict] = None
    group_by: str
    aggregation: ReportAggregation = ReportAggregation.COUNT
    measure_field: Optional[str] = None


class ReportFrequency(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class ScheduledReport(SQLModel, table=True):
    __tablename__ = "scheduled_reports"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    name: str
    # Either a saved definition...
    saved_report_definition_id: Optional[str] = Field(default=None, index=True)
    # ...or one of the existing canned reports (services/analytics_reports_service.py):
    # "attendance" | "fee-collection" | "academic-performance" | "enrollment"
    canned_report_key: Optional[str] = None
    canned_report_params: Optional[str] = None  # JSON-encoded extra params (class_id, campus_id, ...)
    frequency: str = ReportFrequency.WEEKLY.value
    day_of_week: Optional[int] = None  # 0=Monday, used when frequency == weekly
    day_of_month: Optional[int] = None  # used when frequency == monthly
    recipients: str  # JSON-encoded list of email addresses
    enabled: bool = True
    created_by: str
    last_run_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ScheduledReportCreate(SQLModel):
    name: str
    saved_report_definition_id: Optional[str] = None
    canned_report_key: Optional[str] = None
    canned_report_params: Optional[dict] = None
    frequency: ReportFrequency = ReportFrequency.WEEKLY
    day_of_week: Optional[int] = None
    day_of_month: Optional[int] = None
    recipients: list[str]


class ScheduledReportUpdate(SQLModel):
    name: Optional[str] = None
    frequency: Optional[ReportFrequency] = None
    day_of_week: Optional[int] = None
    day_of_month: Optional[int] = None
    recipients: Optional[list[str]] = None
    enabled: Optional[bool] = None


class ScheduledReportRun(SQLModel, table=True):
    __tablename__ = "scheduled_report_runs"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    scheduled_report_id: str = Field(index=True)
    run_at: datetime = Field(default_factory=datetime.utcnow)
    status: str = "sent"  # sent, failed
    recipients_sent: int = 0
    error_message: Optional[str] = None
