"""Self-service Analytics/Reports Router

Curated report types with filters and CSV export — attendance, fee
collection, academic performance, enrollment. Distinct from
routers/finance/reports.py (fixed GL financial statements) and
routers/report_templates.py (report-card HTML template editor).
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from models.user import User, UserRole
from database import get_session
from auth import require_roles
from dependencies import resolve_campus_scope
from services.analytics_reports_service import (
    build_attendance_report, build_fee_collection_report,
    build_academic_performance_report, build_enrollment_report,
    report_to_csv,
)

router = APIRouter(prefix="/reports", tags=["Reports & Analytics"])

VIEW_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR, UserRole.REGISTRAR)


def _csv_response(content: str, filename: str) -> StreamingResponse:
    return StreamingResponse(
        iter([content]), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/attendance", response_model=dict)
async def attendance_report(
    start_date: str,
    end_date: str,
    class_id: Optional[str] = None,
    campus_id: Optional[str] = None,
    current_user: User = Depends(require_roles(*VIEW_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    campus_id = resolve_campus_scope(current_user, campus_id)
    return await build_attendance_report(session, school_id, start_date, end_date, class_id, campus_id)


@router.get("/attendance/export", response_class=StreamingResponse)
async def export_attendance_report(
    start_date: str,
    end_date: str,
    class_id: Optional[str] = None,
    campus_id: Optional[str] = None,
    current_user: User = Depends(require_roles(*VIEW_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    campus_id = resolve_campus_scope(current_user, campus_id)
    report = await build_attendance_report(session, school_id, start_date, end_date, class_id, campus_id)
    csv_content = await report_to_csv(
        session, school_id, "Attendance Report", [["Period", f"{start_date} to {end_date}"]], report
    )
    return _csv_response(csv_content, f"attendance_report_{start_date}_to_{end_date}.csv")


@router.get("/fee-collection", response_model=dict)
async def fee_collection_report(
    start_date: str,
    end_date: str,
    class_id: Optional[str] = None,
    campus_id: Optional[str] = None,
    fee_type: Optional[str] = None,
    current_user: User = Depends(require_roles(*VIEW_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    campus_id = resolve_campus_scope(current_user, campus_id)
    return await build_fee_collection_report(session, school_id, start_date, end_date, class_id, campus_id, fee_type)


@router.get("/fee-collection/export", response_class=StreamingResponse)
async def export_fee_collection_report(
    start_date: str,
    end_date: str,
    class_id: Optional[str] = None,
    campus_id: Optional[str] = None,
    fee_type: Optional[str] = None,
    current_user: User = Depends(require_roles(*VIEW_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    campus_id = resolve_campus_scope(current_user, campus_id)
    report = await build_fee_collection_report(session, school_id, start_date, end_date, class_id, campus_id, fee_type)
    csv_content = await report_to_csv(
        session, school_id, "Fee Collection Report", [["Period", f"{start_date} to {end_date}"]], report
    )
    return _csv_response(csv_content, f"fee_collection_report_{start_date}_to_{end_date}.csv")


@router.get("/academic-performance", response_model=dict)
async def academic_performance_report(
    academic_term_id: str,
    class_id: Optional[str] = None,
    campus_id: Optional[str] = None,
    current_user: User = Depends(require_roles(*VIEW_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    campus_id = resolve_campus_scope(current_user, campus_id)
    return await build_academic_performance_report(session, school_id, academic_term_id, class_id, campus_id)


@router.get("/academic-performance/export", response_class=StreamingResponse)
async def export_academic_performance_report(
    academic_term_id: str,
    class_id: Optional[str] = None,
    campus_id: Optional[str] = None,
    current_user: User = Depends(require_roles(*VIEW_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    campus_id = resolve_campus_scope(current_user, campus_id)
    report = await build_academic_performance_report(session, school_id, academic_term_id, class_id, campus_id)
    csv_content = await report_to_csv(
        session, school_id, "Academic Performance Report", [["Term", academic_term_id]], report
    )
    return _csv_response(csv_content, "academic_performance_report.csv")


@router.get("/enrollment", response_model=dict)
async def enrollment_report(
    class_id: Optional[str] = None,
    campus_id: Optional[str] = None,
    current_user: User = Depends(require_roles(*VIEW_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    campus_id = resolve_campus_scope(current_user, campus_id)
    return await build_enrollment_report(session, school_id, class_id, campus_id)


@router.get("/enrollment/export", response_class=StreamingResponse)
async def export_enrollment_report(
    class_id: Optional[str] = None,
    campus_id: Optional[str] = None,
    current_user: User = Depends(require_roles(*VIEW_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    campus_id = resolve_campus_scope(current_user, campus_id)
    report = await build_enrollment_report(session, school_id, class_id, campus_id)
    csv_content = await report_to_csv(session, school_id, "Enrollment Report", [], report)
    return _csv_response(csv_content, "enrollment_report.csv")
