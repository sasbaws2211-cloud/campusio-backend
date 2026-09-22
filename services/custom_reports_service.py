"""Constrained-but-real ad-hoc report engine for the custom report builder
(models/custom_reports.py) — picks a data source, applies a small fixed
set of filters valid for that source, groups by one field, and aggregates
with count/avg/sum. Deliberately not a generic SQL builder (no raw
user-supplied column/table names ever reach a query) — every field name is
validated against FIELD_MAP before being used to build the SQLAlchemy
query, so this can't be turned into an injection vector.

Also runs due ScheduledReport rows (services/scheduler.py) — resolves
either a SavedReportDefinition or one of the existing canned reports
(services/analytics_reports_service.py), renders a CSV, and emails it via
services/email_service.py's attachment support.
"""
import csv
import io
import json
import logging
from datetime import datetime, date, timedelta
from typing import Any, Dict, Optional

from sqlmodel import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from database import async_session
from models.attendance import Attendance
from models.classroom import Class, Subject
from models.custom_reports import (
    SavedReportDefinition, ScheduledReport, ScheduledReportRun, ReportFrequency,
)
from models.fee import Fee
from models.grade import Grade
from models.staff import Staff
from models.student import Student
from services import analytics_reports_service
from services.email_service import email_service

logger = logging.getLogger(__name__)

FIELD_MAP = {
    "students": {
        "model": Student,
        "filters": {"class_id": Student.class_id, "campus_id": Student.campus_id, "status": Student.status},
        "group_by": {"class_id": Student.class_id, "campus_id": Student.campus_id, "status": Student.status, "gender": Student.gender},
        "measures": {},  # count-only
    },
    "grades": {
        "model": Grade,
        "filters": {"subject_id": Grade.subject_id, "class_id": Grade.class_id, "academic_term_id": Grade.academic_term_id, "assessment_type": Grade.assessment_type},
        "group_by": {"subject_id": Grade.subject_id, "class_id": Grade.class_id, "assessment_type": Grade.assessment_type},
        # "score"/"max_score" stay available for a "total points" style
        # report (sum), but averaging raw score alone mixes assessments
        # with different max_score into a meaningless number — the same
        # bug already fixed in services/assignment_performance.py and
        # routers/extra_classes.py. "percentage" is each Grade row's own
        # score/max_score already normalized, so avg(percentage) is a
        # correct "average grade by <group>" a report builder can trust
        # (nullif guards a max_score of 0 rather than erroring the query).
        "measures": {
            "score": Grade.score,
            "max_score": Grade.max_score,
            "percentage": Grade.score / func.nullif(Grade.max_score, 0) * 100,
        },
    },
    "attendance": {
        "model": Attendance,
        "filters": {"class_id": Attendance.class_id, "academic_term_id": Attendance.academic_term_id, "status": Attendance.status},
        "group_by": {"class_id": Attendance.class_id, "status": Attendance.status},
        "measures": {},
    },
    "fees": {
        "model": Fee,
        "filters": {"academic_term_id": Fee.academic_term_id, "status": Fee.status},
        "group_by": {"status": Fee.status, "academic_term_id": Fee.academic_term_id},
        "measures": {"amount_due": Fee.amount_due, "amount_paid": Fee.amount_paid},
    },
    "staff": {
        "model": Staff,
        "filters": {"staff_type": Staff.staff_type, "status": Staff.status, "department": Staff.department, "campus_id": Staff.campus_id},
        "group_by": {"staff_type": Staff.staff_type, "status": Staff.status, "department": Staff.department, "gender": Staff.gender},
        "measures": {},
    },
}

# Row-level (not aggregated) export — the shape a government/EMIS submission
# actually wants: one row per student/staff record with selected columns,
# not a group-by count. Deliberately a separate allow-list from FIELD_MAP's
# filters/group_by/measures (those pick a single column to bucket by; this
# picks a SET of columns to include verbatim), but reuses the same model +
# filters from FIELD_MAP so both tools stay consistent about what a school
# can scope an export to.
EXPORT_COLUMNS = {
    "students": ["student_id", "first_name", "last_name", "other_names", "date_of_birth", "gender", "admission_date", "class_id", "campus_id", "nationality", "status"],
    "staff": ["staff_id", "first_name", "last_name", "other_names", "date_of_birth", "gender", "staff_type", "position", "department", "qualification", "date_joined", "status", "ssnit_number"],
    # Row-level extracts for the two sources the custom report builder's
    # ad-hoc/group-by tool already supports (see FIELD_MAP above) but the
    # row-level export previously didn't — grades and attendance.
    "grades": ["student_id", "subject_id", "assessment_type", "score", "max_score", "academic_term_id"],
    "attendance": ["student_id", "class_id", "attendance_date", "status"],
}

# Columns whose raw value is a foreign-key id that should be resolved to a
# human-readable label in the export, same resolution _label_resolver
# already does for group_by values.
_EXPORT_LABEL_COLUMNS = {"class_id", "campus_id", "subject_id"}


def _apply_campus_scope(query, model, caller_campus_id: Optional[str]):
    """Forces a campus-scoped caller's own campus_id onto the query,
    independent of whatever (if anything) the caller's own `filters` dict
    requested. Previously campus_id was only ever an OPTIONAL filter key a
    caller could choose to pass — a campus-scoped admin who simply omitted
    it (or a client that never offered the field) could pull every campus's
    data through the ad-hoc report builder or the raw CSV export, the
    highest-blast-radius instance of this module's missing campus scoping.
    `model` with its own campus_id column (Student, Staff, Fee) is filtered
    directly; a model with none (Grade, Attendance — both student_id-keyed)
    is scoped by joining to Student.campus_id instead."""
    if not caller_campus_id:
        return query
    if hasattr(model, "campus_id"):
        return query.where(model.campus_id == caller_campus_id)
    if hasattr(model, "student_id"):
        return query.join(Student, Student.id == model.student_id).where(Student.campus_id == caller_campus_id)
    return query


async def run_row_export(session: AsyncSession, school_id: str, source: str, filters: Optional[Dict[str, Any]], columns: list, caller_campus_id: Optional[str] = None) -> list[dict]:
    if source not in EXPORT_COLUMNS:
        raise ValueError(f"Row-level export is not available for source '{source}'")
    spec = FIELD_MAP[source]
    model = spec["model"]
    available = EXPORT_COLUMNS[source]

    if not columns:
        raise ValueError("At least one column is required")
    invalid = [c for c in columns if c not in available]
    if invalid:
        raise ValueError(f"Invalid column(s) for source '{source}': {invalid}")

    query = select(model).where(model.school_id == school_id)
    for key, value in (filters or {}).items():
        if key in spec["filters"] and value not in (None, ""):
            query = query.where(spec["filters"][key] == value)
    query = _apply_campus_scope(query, model, caller_campus_id)

    rows = (await session.execute(query)).scalars().all()
    result = []
    for row in rows:
        record = {}
        for col in columns:
            value = getattr(row, col, None)
            if value is None:
                record[col] = ""
            elif col in _EXPORT_LABEL_COLUMNS:
                record[col] = await _label_resolver(session, school_id, col, value)
            elif hasattr(value, "value"):  # native-enum member
                record[col] = value.value
            else:
                record[col] = str(value)
        result.append(record)
    return result


async def _label_resolver(session: AsyncSession, school_id: str, field: str, value: str) -> str:
    if value is None:
        return "—"
    # Several group_by columns (Student.status, Grade.assessment_type, ...) are
    # native-enum-typed, so SQLAlchemy hands back the Python Enum member, not
    # its string value — str(member) gives the ugly "ClassName.MEMBER" repr.
    if hasattr(value, "value"):
        value = value.value
    if field == "class_id":
        cls = await session.get(Class, value)
        return cls.name if cls else value
    if field == "subject_id":
        subj = await session.get(Subject, value)
        return subj.name if subj else value
    if field == "campus_id":
        from models.campus import Campus
        campus = await session.get(Campus, value)
        return campus.name if campus else value
    return str(value).replace("_", " ")


async def run_ad_hoc_report(session: AsyncSession, school_id: str, source: str, filters: Optional[Dict[str, Any]], group_by: str, aggregation: str, measure_field: Optional[str], caller_campus_id: Optional[str] = None) -> dict:
    if source not in FIELD_MAP:
        raise ValueError(f"Unknown report source: {source}")
    spec = FIELD_MAP[source]
    model = spec["model"]

    if group_by not in spec["group_by"]:
        raise ValueError(f"'{group_by}' is not a valid group_by field for source '{source}'")
    group_col = spec["group_by"][group_by]

    if aggregation != "count":
        if not measure_field or measure_field not in spec["measures"]:
            raise ValueError(f"'{measure_field}' is not a valid measure_field for source '{source}'")
        if source == "grades" and measure_field == "percentage" and aggregation == "avg":
            # AVG(score/max_score*100) is a mean of ratios, which diverges
            # from the report card's weighted total whenever max_score
            # varies within the group (same class of bug already fixed in
            # services/analytics.py and services/assignment_performance.py)
            # — SUM(score)/SUM(max_score) is the correct point-weighted
            # average, matching how compute_subject_ges_totals sums points
            # before dividing rather than averaging per-row percentages.
            agg_func = func.sum(Grade.score) / func.nullif(func.sum(Grade.max_score), 0) * 100
        else:
            measure_col = spec["measures"][measure_field]
            agg_func = {"avg": func.avg, "sum": func.sum}[aggregation](measure_col)
    else:
        agg_func = func.count(model.id)

    query = select(group_col, agg_func).where(model.school_id == school_id)
    for key, value in (filters or {}).items():
        if key in spec["filters"] and value not in (None, ""):
            query = query.where(spec["filters"][key] == value)
    query = _apply_campus_scope(query, model, caller_campus_id)
    query = query.group_by(group_col)

    rows = (await session.execute(query)).all()
    result_rows = []
    for group_value, agg_value in rows:
        label = await _label_resolver(session, school_id, group_by, group_value)
        result_rows.append({"group": label, "group_key": group_value, "value": round(float(agg_value), 2) if agg_value is not None else 0})
    result_rows.sort(key=lambda r: -r["value"])

    return {"source": source, "group_by": group_by, "aggregation": aggregation, "measure_field": measure_field, "rows": result_rows}


def report_rows_to_csv(rows: list, columns: list) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in columns})
    return buffer.getvalue()


async def _run_canned_report(session: AsyncSession, school_id: str, key: str, params: dict) -> Optional[dict]:
    """Each canned builder in services/analytics_reports_service.py has its
    own signature (date-range vs term-scoped vs snapshot-only) — dispatch
    explicitly rather than assuming a uniform call shape."""
    today = date.today()
    if key == "attendance":
        start_date = params.get("start_date") or (today - timedelta(days=30)).isoformat()
        end_date = params.get("end_date") or today.isoformat()
        return await analytics_reports_service.build_attendance_report(session, school_id, start_date, end_date, params.get("class_id"), params.get("campus_id"))
    if key == "fee-collection":
        start_date = params.get("start_date") or (today - timedelta(days=30)).isoformat()
        end_date = params.get("end_date") or today.isoformat()
        return await analytics_reports_service.build_fee_collection_report(session, school_id, start_date, end_date, params.get("class_id"), params.get("campus_id"), params.get("fee_type"))
    if key == "academic-performance":
        if not params.get("academic_term_id"):
            return None
        return await analytics_reports_service.build_academic_performance_report(session, school_id, params["academic_term_id"], params.get("class_id"), params.get("campus_id"))
    if key == "enrollment":
        return await analytics_reports_service.build_enrollment_report(session, school_id, params.get("class_id"), params.get("campus_id"))
    return None


CANNED_REPORT_KEYS = ("attendance", "fee-collection", "academic-performance", "enrollment")


def _due(scheduled: ScheduledReport, now: datetime) -> bool:
    if not scheduled.enabled:
        return False
    if not scheduled.last_run_at:
        return True
    elapsed = now - scheduled.last_run_at
    if scheduled.frequency == ReportFrequency.DAILY.value:
        return elapsed >= timedelta(hours=23)
    if scheduled.frequency == ReportFrequency.WEEKLY.value:
        if scheduled.day_of_week is not None and now.weekday() != scheduled.day_of_week:
            return False
        return elapsed >= timedelta(days=6)
    if scheduled.frequency == ReportFrequency.MONTHLY.value:
        if scheduled.day_of_month is not None and now.day != scheduled.day_of_month:
            return False
        return elapsed >= timedelta(days=27)
    return False


async def _build_report_csv(session: AsyncSession, school_id: str, scheduled: ScheduledReport) -> Optional[str]:
    if scheduled.saved_report_definition_id:
        definition = await session.get(SavedReportDefinition, scheduled.saved_report_definition_id)
        if not definition:
            return None
        filters = json.loads(definition.filters) if definition.filters else None
        result = await run_ad_hoc_report(session, school_id, definition.source, filters, definition.group_by, definition.aggregation, definition.measure_field)
        return report_rows_to_csv(result["rows"], ["group", "value"])

    if scheduled.canned_report_key and scheduled.canned_report_key in CANNED_REPORT_KEYS:
        params = json.loads(scheduled.canned_report_params) if scheduled.canned_report_params else {}
        result = await _run_canned_report(session, school_id, scheduled.canned_report_key, params)
        if result is None:
            return None
        rows = result.get("rows", [])
        columns = list(rows[0].keys()) if rows else []
        return report_rows_to_csv(rows, columns)

    return None


async def run_due_scheduled_reports(session: AsyncSession, school_id: Optional[str] = None) -> Dict:
    """school_id=None (the nightly sweep's usage) checks every school's due
    reports; a caller-scoped manual trigger must pass its own school_id, or
    it would fire — and prematurely email out — every OTHER school's due
    reports too."""
    now = datetime.utcnow()
    query = select(ScheduledReport).where(ScheduledReport.enabled == True)  # noqa: E712
    if school_id:
        query = query.where(ScheduledReport.school_id == school_id)
    scheduled_reports = (await session.execute(query)).scalars().all()

    sent = 0
    for scheduled in scheduled_reports:
        if not _due(scheduled, now):
            continue
        try:
            csv_content = await _build_report_csv(session, scheduled.school_id, scheduled)
            if csv_content is None:
                session.add(ScheduledReportRun(scheduled_report_id=scheduled.id, status="failed", error_message="Report source not found"))
                continue

            recipients = json.loads(scheduled.recipients)
            attachment_b64 = __import__("base64").b64encode(csv_content.encode()).decode()
            email_result = await email_service.send_email(
                to=recipients, subject=f"Scheduled Report: {scheduled.name}",
                html_body=f"<p>Attached is your scheduled report: <strong>{scheduled.name}</strong>.</p>",
                attachments=[{"filename": f"{scheduled.name.replace(' ', '_')}.csv", "content": attachment_b64}],
            )
            scheduled.last_run_at = now
            session.add(scheduled)
            if email_result.get("success"):
                sent += 1
                session.add(ScheduledReportRun(scheduled_report_id=scheduled.id, status="sent", recipients_sent=len(recipients)))
            else:
                session.add(ScheduledReportRun(scheduled_report_id=scheduled.id, status="failed", error_message=email_result.get("error")))
        except Exception as e:
            logger.error(f"Scheduled report {scheduled.id} failed: {e}")
            session.add(ScheduledReportRun(scheduled_report_id=scheduled.id, status="failed", error_message=str(e)))

    await session.commit()
    return {"reports_checked": len(scheduled_reports), "reports_sent": sent}


async def run_scheduled_reports_sweep() -> None:
    """Called by services/scheduler.py — checks every school's scheduled reports."""
    async with async_session() as session:
        result = await run_due_scheduled_reports(session)
        logger.info(f"Scheduled reports sweep: {result['reports_sent']} report(s) emailed out of {result['reports_checked']} due-checked")
