"""Strategic reporting layer beyond the existing attendance/finance/
operational reports (routers/reports_analytics.py) and learning-analytics
dashboard (routers/analytics.py): retention/dropout, enrollment trends,
teacher workload, fee collection forecasting, and cross-campus comparison.
Read-only GET endpoints, all school-scoped."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_roles
from database import get_session
from models.attendance import Attendance, AttendanceStatus
from models.campus import Campus
from models.fee import Fee, FeeInstallment, InstallmentStatus, PaymentStatus
from models.grade import Grade
from models.staff import Staff
from models.student import Student, StudentStatus, StudentStatusEvent
from models.timetable import Timetable
from models.user import User, UserRole
from services.analytics import _weighted_gpa
from services.fee_reminder_service import is_fee_overdue

router = APIRouter(prefix="/strategic-reports", tags=["Strategic Reports"])

STAFF_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR)


def _school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


# ── Retention / Dropout ─────────────────────────────────────────────────

@router.get("/retention", response_model=dict)
async def retention_report(
    months: int = Query(12, le=36),
    current_user: User = Depends(require_roles(*STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    students = (await session.execute(select(Student).where(Student.school_id == school_id))).scalars().all()

    by_status = defaultdict(int)
    for s in students:
        by_status[s.status] += 1

    total = len(students)
    retained = by_status.get(StudentStatus.ACTIVE.value, 0) + by_status.get(StudentStatus.GRADUATED.value, 0)
    retention_rate = round(retained / total * 100, 1) if total else 0

    since = datetime.utcnow() - timedelta(days=months * 30)
    events = (await session.execute(
        select(StudentStatusEvent).where(
            StudentStatusEvent.school_id == school_id,
            StudentStatusEvent.to_status.in_([StudentStatus.WITHDRAWN.value, StudentStatus.TRANSFERRED.value]),
            StudentStatusEvent.created_at >= since,
        )
    )).scalars().all()

    by_month = defaultdict(int)
    by_reason = defaultdict(int)
    for e in events:
        by_month[e.created_at.strftime("%Y-%m")] += 1
        by_reason[e.reason or "unspecified"] += 1

    return {
        "total_students": total,
        "retention_rate": retention_rate,
        "by_status": dict(by_status),
        "dropouts_by_month": dict(sorted(by_month.items())),
        "dropouts_by_reason": dict(sorted(by_reason.items(), key=lambda kv: -kv[1])),
        "dropout_count": len(events),
    }


# ── Enrollment Trends ────────────────────────────────────────────────────

@router.get("/enrollment-trends", response_model=dict)
async def enrollment_trends(
    months: int = Query(12, le=36),
    campus_id: Optional[str] = None,
    current_user: User = Depends(require_roles(*STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    since = (date.today() - timedelta(days=months * 30)).isoformat()
    query = select(Student).where(Student.school_id == school_id, Student.admission_date >= since)
    if campus_id:
        query = query.where(Student.campus_id == campus_id)
    students = (await session.execute(query)).scalars().all()

    by_month = defaultdict(int)
    by_campus = defaultdict(int)
    by_admission_type = defaultdict(int)
    for s in students:
        month = s.admission_date[:7] if s.admission_date and len(s.admission_date) >= 7 else "unknown"
        by_month[month] += 1
        by_campus[s.campus_id or "unassigned"] += 1
        by_admission_type[s.admission_type] += 1

    campuses = {c.id: c.name for c in (await session.execute(select(Campus).where(Campus.school_id == school_id))).scalars().all()}
    by_campus_named = {campuses.get(cid, cid): count for cid, count in by_campus.items()}

    return {
        "new_enrollments": len(students),
        "by_month": dict(sorted(by_month.items())),
        "by_campus": by_campus_named,
        "by_admission_type": dict(by_admission_type),
    }


# ── Teacher Workload ─────────────────────────────────────────────────────

@router.get("/teacher-workload", response_model=dict)
async def teacher_workload(
    academic_term_id: str,
    current_user: User = Depends(require_roles(*STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    timetable_rows = (await session.execute(
        select(Timetable).where(Timetable.school_id == school_id, Timetable.academic_term_id == academic_term_id)
    )).scalars().all()
    if not timetable_rows:
        return {"teachers": []}

    teacher_ids = {t.teacher_id for t in timetable_rows}
    staff_map = {s.id: s for s in (await session.execute(select(Staff).where(Staff.id.in_(teacher_ids)))).scalars().all()}

    per_teacher = defaultdict(lambda: {"periods_per_week": 0, "classes": set(), "subjects": set(), "students": 0})
    for t in timetable_rows:
        entry = per_teacher[t.teacher_id]
        entry["periods_per_week"] += 1
        entry["classes"].add(t.class_id)
        entry["subjects"].add(t.subject_id)

    class_sizes = {}
    all_class_ids = {cid for e in per_teacher.values() for cid in e["classes"]}
    if all_class_ids:
        rows = (await session.execute(
            select(Student.class_id, func.count(Student.id)).where(Student.class_id.in_(all_class_ids), Student.status == "active").group_by(Student.class_id)
        )).all()
        class_sizes = {r[0]: r[1] for r in rows}

    teachers = []
    for teacher_id, entry in per_teacher.items():
        staff = staff_map.get(teacher_id)
        students_taught = sum(class_sizes.get(cid, 0) for cid in entry["classes"])
        grading_load = 0
        if staff:
            # Grade.recorded_by is keyed on Staff.id, not User.id (see
            # routers/teacher/grades.py::resolve_staff and
            # routers/grades.py::_recorded_by_id) — comparing against
            # staff.user_id here silently missed every grade recorded via
            # the teacher portal, which stores Staff.id.
            grading_load = (await session.execute(
                select(func.count(Grade.id)).where(Grade.school_id == school_id, Grade.academic_term_id == academic_term_id, Grade.recorded_by == staff.id)
            )).scalar() or 0
        teachers.append({
            "teacher_id": teacher_id,
            "teacher_name": f"{staff.first_name} {staff.last_name}" if staff else "Unknown",
            "periods_per_week": entry["periods_per_week"],
            "classes_taught": len(entry["classes"]),
            "subjects_taught": len(entry["subjects"]),
            "students_taught": students_taught,
            "grades_recorded": grading_load,
        })
    teachers.sort(key=lambda t: -t["periods_per_week"])

    return {"academic_term_id": academic_term_id, "teachers": teachers}


# ── Fee Collection Forecasting ───────────────────────────────────────────

@router.get("/fee-forecast", response_model=dict)
async def fee_forecast(
    months: int = Query(3, le=12),
    current_user: User = Depends(require_roles(*STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    today = date.today()

    past_installments = (await session.execute(
        select(FeeInstallment).where(FeeInstallment.school_id == school_id, FeeInstallment.due_date < today.isoformat())
    )).scalars().all()
    paid_on_time = sum(1 for i in past_installments if i.status == InstallmentStatus.PAID.value)
    collection_rate = round(paid_on_time / len(past_installments), 3) if past_installments else 0.85  # fall back to a conservative default with no history

    horizon_end = (today + timedelta(days=months * 30)).isoformat()
    upcoming = (await session.execute(
        select(FeeInstallment).where(
            FeeInstallment.school_id == school_id, FeeInstallment.due_date >= today.isoformat(), FeeInstallment.due_date <= horizon_end,
            FeeInstallment.status.in_([InstallmentStatus.PENDING.value, InstallmentStatus.PARTIAL.value, InstallmentStatus.OVERDUE.value]),
        )
    )).scalars().all()

    by_month = defaultdict(float)
    for i in upcoming:
        outstanding = i.amount_due - i.amount_paid
        if outstanding <= 0:
            continue
        month = i.due_date[:7] if i.due_date and len(i.due_date) >= 7 else "unknown"
        by_month[month] += outstanding

    projected = {
        month: {"billed": round(amount, 2), "projected_collected": round(amount * collection_rate, 2)}
        for month, amount in sorted(by_month.items())
    }
    total_billed = sum(v["billed"] for v in projected.values())
    total_projected = sum(v["projected_collected"] for v in projected.values())

    # Computed from due-date math (services.fee_reminder_service.is_fee_overdue)
    # rather than Fee.status == OVERDUE — that status only gets set by a
    # narrow, opt-in nightly sweep or an unscheduled manual endpoint, so it
    # can't be trusted as this KPI's source of truth.
    candidate_fees = (await session.execute(
        select(Fee).where(
            Fee.school_id == school_id,
            Fee.status.in_([PaymentStatus.PENDING.value, PaymentStatus.PARTIAL.value, PaymentStatus.OVERDUE.value]),
        )
    )).scalars().all()
    overdue_now = 0.0
    for fee in candidate_fees:
        if await is_fee_overdue(session, fee):
            overdue_now += fee.amount_due - fee.amount_paid - fee.discount

    return {
        "historical_on_time_collection_rate": collection_rate,
        "current_overdue_balance": round(overdue_now or 0, 2),
        "by_month": projected,
        "total_billed": round(total_billed, 2),
        "total_projected_collected": round(total_projected, 2),
    }


# ── Cross-Campus Comparison ──────────────────────────────────────────────

@router.get("/campus-comparison", response_model=dict)
async def campus_comparison(
    academic_term_id: str,
    current_user: User = Depends(require_roles(*STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    campuses = (await session.execute(select(Campus).where(Campus.school_id == school_id))).scalars().all()
    if not campuses:
        return {"campuses": []}

    results = []
    for campus in campuses:
        students = (await session.execute(
            select(Student).where(Student.school_id == school_id, Student.campus_id == campus.id, Student.status == "active")
        )).scalars().all()
        student_ids = [s.id for s in students]
        staff_count = (await session.execute(
            select(func.count(Staff.id)).where(Staff.school_id == school_id, Staff.campus_id == campus.id)
        )).scalar() or 0

        gpas = []
        for sid in student_ids:
            gpa = await _weighted_gpa(session, sid, school_id, academic_term_id)
            if gpa is not None:
                gpas.append(gpa)
        avg_gpa = round(sum(gpas) / len(gpas), 2) if gpas else None

        if student_ids:
            present = (await session.execute(
                select(func.count(Attendance.id)).where(
                    Attendance.student_id.in_(student_ids), Attendance.school_id == school_id, Attendance.academic_term_id == academic_term_id,
                    Attendance.status.in_([AttendanceStatus.PRESENT, AttendanceStatus.LATE]),
                )
            )).scalar() or 0
            total = (await session.execute(
                select(func.count(Attendance.id)).where(
                    Attendance.student_id.in_(student_ids), Attendance.school_id == school_id, Attendance.academic_term_id == academic_term_id,
                )
            )).scalar() or 0
            attendance_rate = round(present / total * 100, 1) if total else None

            fees = (await session.execute(
                select(Fee).where(Fee.student_id.in_(student_ids), Fee.school_id == school_id, Fee.academic_term_id == academic_term_id)
            )).scalars().all()
            total_due = sum(f.amount_due - f.discount for f in fees)
            total_paid = sum(f.amount_paid for f in fees)
            fee_collection_rate = round(total_paid / total_due * 100, 1) if total_due else None
        else:
            attendance_rate = fee_collection_rate = None

        results.append({
            "campus_id": campus.id, "campus_name": campus.name,
            "student_count": len(students), "staff_count": staff_count,
            "average_gpa": avg_gpa, "attendance_rate": attendance_rate, "fee_collection_rate": fee_collection_rate,
        })

    return {"academic_term_id": academic_term_id, "campuses": results}
