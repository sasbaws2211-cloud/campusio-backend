"""Custom report builder + scheduled/emailed reports — see
models/custom_reports.py and services/custom_reports_service.py."""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from auth import require_roles
from database import get_session
from models.custom_reports import (
    SavedReportDefinition, SavedReportDefinitionCreate, SavedReportDefinitionUpdate, AdHocReportRequest,
    ScheduledReport, ScheduledReportCreate, ScheduledReportUpdate, ScheduledReportRun,
)
from models.school import School, AcademicTerm
from models.strategic_goals import StrategicGoal
from models.user import User, UserRole
from services.analytics_reports_service import (
    build_attendance_report, build_fee_collection_report,
    build_academic_performance_report, build_enrollment_report,
)
from services.board_pack_pdf_service import BoardPackPDFService
from services.custom_reports_service import (
    run_ad_hoc_report, run_due_scheduled_reports, FIELD_MAP, CANNED_REPORT_KEYS,
    EXPORT_COLUMNS, run_row_export, report_rows_to_csv,
)

router = APIRouter(prefix="/custom-reports", tags=["Custom Reports"])

WRITE_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR)


def _school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


@router.get("/field-map", response_model=dict)
async def get_field_map(current_user: User = Depends(require_roles(*WRITE_ROLES))):
    """Tells the frontend builder which filter/group-by/measure fields are
    valid for each source, so it can render the right pickers."""
    return {
        source: {
            "filters": list(spec["filters"].keys()),
            "group_by": list(spec["group_by"].keys()),
            "measures": list(spec["measures"].keys()),
        }
        for source, spec in FIELD_MAP.items()
    }


@router.post("/run", response_model=dict)
async def run_ad_hoc(payload: AdHocReportRequest, current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    try:
        return await run_ad_hoc_report(session, school_id, payload.source.value, payload.filters, payload.group_by, payload.aggregation.value, payload.measure_field, caller_campus_id=current_user.campus_id)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


# ── Generic row-level export (government/EMIS-style extracts) ───────────

@router.get("/export/columns", response_model=dict)
async def get_export_columns(current_user: User = Depends(require_roles(*WRITE_ROLES))):
    """Which sources support a row-level (not aggregated) export, and which
    columns each one can include — for a frontend column-picker."""
    return {source: columns for source, columns in EXPORT_COLUMNS.items()}


@router.get("/export.csv")
async def export_rows_csv(
    source: str, columns: str, filters: Optional[str] = None,
    current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session),
):
    """A configurable CSV extract of raw student/staff records — the shape
    a ministry-of-education/EMIS portal submission actually wants (one row
    per record), as opposed to /run's aggregated group-by counts. `columns`
    is a comma-separated list from GET /export/columns; `filters` is an
    optional JSON-encoded object using the same filter keys as /field-map."""
    school_id = _school_id(current_user)
    column_list = [c.strip() for c in columns.split(",") if c.strip()]
    filter_dict = json.loads(filters) if filters else None
    try:
        rows = await run_row_export(session, school_id, source, filter_dict, column_list, caller_campus_id=current_user.campus_id)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    csv_content = report_rows_to_csv(rows, column_list)
    filename = f"{source}_export_{datetime.utcnow().strftime('%Y%m%d')}.csv"
    return StreamingResponse(iter([csv_content]), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={filename}"})


# ── Board Reporting Package (PDF) ─────────────────────────────────────────

def _pack_section(title: str, report: dict, max_rows: int = 25) -> dict:
    rows = report.get("rows", [])
    columns = list(rows[0].keys()) if rows else []
    return {
        "title": title, "summary": report.get("summary", {}),
        "columns": columns, "rows": rows[:max_rows],
        "truncated": len(rows) > max_rows, "total_rows": len(rows),
    }


@router.get("/board-pack")
async def get_board_pack(
    start_date: str, end_date: str, academic_term_id: str,
    class_id: Optional[str] = None, campus_id: Optional[str] = None,
    current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session),
):
    """On-demand board reporting package — one PDF bundling attendance, fee
    collection, academic performance, enrollment (all from
    services/analytics_reports_service.py, unchanged) plus strategic-goals
    progress. See services/board_pack_pdf_service.py; not wired into the
    scheduler/email path — generated on demand only."""
    school_id = _school_id(current_user)

    school = await session.get(School, school_id)
    academic_term = await session.get(AcademicTerm, academic_term_id)
    if not academic_term or academic_term.school_id != school_id:
        raise HTTPException(status_code=404, detail="Academic term not found")

    attendance_report = await build_attendance_report(session, school_id, start_date, end_date, class_id, campus_id)
    fee_report = await build_fee_collection_report(session, school_id, start_date, end_date, class_id, campus_id)
    academic_report = await build_academic_performance_report(session, school_id, academic_term_id, class_id, campus_id)
    enrollment_report = await build_enrollment_report(session, school_id, class_id, campus_id)

    goals_rows = (await session.execute(
        select(StrategicGoal).where(StrategicGoal.school_id == school_id).order_by(StrategicGoal.target_date)
    )).scalars().all()
    goals = [
        {
            "title": g.title, "category": g.category, "target_value": g.target_value,
            "current_value": g.current_value, "unit": g.unit, "status": g.status, "target_date": g.target_date,
        }
        for g in goals_rows
    ]

    pack_data = {
        "school_name": school.name if school else "",
        "generated_date": datetime.utcnow().strftime("%d %B %Y"),
        "period_start": start_date,
        "period_end": end_date,
        "academic_term_name": f"{academic_term.academic_year} — Term {academic_term.term}",
        "sections": [
            _pack_section("Attendance", attendance_report),
            _pack_section("Fee Collection", fee_report),
            _pack_section("Academic Performance", academic_report),
            _pack_section("Enrollment", enrollment_report),
        ],
        "goals": goals,
    }

    try:
        pdf_bytes = BoardPackPDFService().generate_pdf(pack_data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate board pack PDF: {str(e)}")

    filename = f"board-pack-{date.today().isoformat()}.pdf"
    return StreamingResponse(
        iter([pdf_bytes]), media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ── Saved report definitions ─────────────────────────────────────────────

@router.post("/definitions", response_model=dict)
async def create_definition(payload: SavedReportDefinitionCreate, current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    item = SavedReportDefinition(
        school_id=school_id, created_by=current_user.id, name=payload.name, source=payload.source.value,
        filters=json.dumps(payload.filters) if payload.filters else None, group_by=payload.group_by,
        aggregation=payload.aggregation.value, measure_field=payload.measure_field,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _def_to_dict(item)


def _def_to_dict(item: SavedReportDefinition) -> dict:
    return {
        "id": item.id, "name": item.name, "source": item.source,
        "filters": json.loads(item.filters) if item.filters else None,
        "group_by": item.group_by, "aggregation": item.aggregation, "measure_field": item.measure_field,
        "created_at": item.created_at,
    }


@router.get("/definitions", response_model=List[dict])
async def list_definitions(current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    result = await session.execute(select(SavedReportDefinition).where(SavedReportDefinition.school_id == school_id).order_by(SavedReportDefinition.created_at.desc()))
    return [_def_to_dict(d) for d in result.scalars().all()]


@router.put("/definitions/{definition_id}", response_model=dict)
async def update_definition(definition_id: str, payload: SavedReportDefinitionUpdate, current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    item = (await session.execute(select(SavedReportDefinition).where(SavedReportDefinition.id == definition_id, SavedReportDefinition.school_id == school_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Report definition not found")
    update_data = payload.model_dump(exclude_unset=True)
    if "filters" in update_data:
        update_data["filters"] = json.dumps(update_data["filters"]) if update_data["filters"] else None
    if "aggregation" in update_data and update_data["aggregation"] is not None:
        update_data["aggregation"] = update_data["aggregation"].value
    for key, value in update_data.items():
        setattr(item, key, value)
    item.updated_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _def_to_dict(item)


@router.delete("/definitions/{definition_id}", response_model=dict)
async def delete_definition(definition_id: str, current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    item = (await session.execute(select(SavedReportDefinition).where(SavedReportDefinition.id == definition_id, SavedReportDefinition.school_id == school_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Report definition not found")
    await session.delete(item)
    await session.commit()
    return {"success": True, "message": "Report definition deleted"}


@router.post("/definitions/{definition_id}/run", response_model=dict)
async def run_saved_definition(definition_id: str, current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    item = (await session.execute(select(SavedReportDefinition).where(SavedReportDefinition.id == definition_id, SavedReportDefinition.school_id == school_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Report definition not found")
    filters = json.loads(item.filters) if item.filters else None
    return await run_ad_hoc_report(session, school_id, item.source, filters, item.group_by, item.aggregation, item.measure_field, caller_campus_id=current_user.campus_id)


# ── Scheduled reports ────────────────────────────────────────────────────

def _scheduled_to_dict(item: ScheduledReport) -> dict:
    return {
        "id": item.id, "name": item.name,
        "saved_report_definition_id": item.saved_report_definition_id,
        "canned_report_key": item.canned_report_key,
        "canned_report_params": json.loads(item.canned_report_params) if item.canned_report_params else None,
        "frequency": item.frequency, "day_of_week": item.day_of_week, "day_of_month": item.day_of_month,
        "recipients": json.loads(item.recipients), "enabled": item.enabled,
        "last_run_at": item.last_run_at, "created_at": item.created_at,
    }


@router.post("/scheduled", response_model=dict)
async def create_scheduled_report(payload: ScheduledReportCreate, current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    if not payload.saved_report_definition_id and not payload.canned_report_key:
        raise HTTPException(status_code=422, detail="Provide either saved_report_definition_id or canned_report_key")
    if payload.canned_report_key and payload.canned_report_key not in CANNED_REPORT_KEYS:
        raise HTTPException(status_code=422, detail=f"canned_report_key must be one of {CANNED_REPORT_KEYS}")
    if not payload.recipients:
        raise HTTPException(status_code=422, detail="At least one recipient email is required")

    item = ScheduledReport(
        school_id=school_id, created_by=current_user.id, name=payload.name,
        saved_report_definition_id=payload.saved_report_definition_id, canned_report_key=payload.canned_report_key,
        canned_report_params=json.dumps(payload.canned_report_params) if payload.canned_report_params else None,
        frequency=payload.frequency.value, day_of_week=payload.day_of_week, day_of_month=payload.day_of_month,
        recipients=json.dumps(payload.recipients),
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _scheduled_to_dict(item)


@router.get("/scheduled", response_model=List[dict])
async def list_scheduled_reports(current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    result = await session.execute(select(ScheduledReport).where(ScheduledReport.school_id == school_id).order_by(ScheduledReport.created_at.desc()))
    return [_scheduled_to_dict(s) for s in result.scalars().all()]


@router.put("/scheduled/{scheduled_id}", response_model=dict)
async def update_scheduled_report(scheduled_id: str, payload: ScheduledReportUpdate, current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    item = (await session.execute(select(ScheduledReport).where(ScheduledReport.id == scheduled_id, ScheduledReport.school_id == school_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Scheduled report not found")
    update_data = payload.model_dump(exclude_unset=True)
    if "frequency" in update_data and update_data["frequency"] is not None:
        update_data["frequency"] = update_data["frequency"].value
    if "recipients" in update_data and update_data["recipients"] is not None:
        update_data["recipients"] = json.dumps(update_data["recipients"])
    for key, value in update_data.items():
        setattr(item, key, value)
    item.updated_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _scheduled_to_dict(item)


@router.delete("/scheduled/{scheduled_id}", response_model=dict)
async def delete_scheduled_report(scheduled_id: str, current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    item = (await session.execute(select(ScheduledReport).where(ScheduledReport.id == scheduled_id, ScheduledReport.school_id == school_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Scheduled report not found")
    await session.delete(item)
    await session.commit()
    return {"success": True, "message": "Scheduled report deleted"}


@router.post("/scheduled/run-due", response_model=dict)
async def run_due_now(current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    """Manual trigger — runs whatever is currently due across this school's
    scheduled reports, same logic the nightly sweep uses. Must pass school_id
    — without it this would also fire every OTHER school's due reports."""
    school_id = _school_id(current_user)
    return await run_due_scheduled_reports(session, school_id=school_id)


@router.get("/scheduled/{scheduled_id}/runs", response_model=List[dict])
async def list_runs(scheduled_id: str, current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    scheduled = (await session.execute(select(ScheduledReport).where(ScheduledReport.id == scheduled_id, ScheduledReport.school_id == school_id))).scalar_one_or_none()
    if not scheduled:
        raise HTTPException(status_code=404, detail="Scheduled report not found")
    result = await session.execute(select(ScheduledReportRun).where(ScheduledReportRun.scheduled_report_id == scheduled_id).order_by(ScheduledReportRun.run_at.desc()).limit(50))
    return [
        {"id": r.id, "run_at": r.run_at, "status": r.status, "recipients_sent": r.recipients_sent, "error_message": r.error_message}
        for r in result.scalars().all()
    ]
