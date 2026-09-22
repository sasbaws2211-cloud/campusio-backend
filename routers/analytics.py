"""Analytics API Endpoints - Student and class performance insights.

Rewritten from scratch alongside services/analytics.py — the original had
int-typed path/query params against a UUID-string schema (every real
student_id/class_id/academic_term_id would 422 before the handler even
ran), called `require_roles(current_user, [...])` as a plain function
(require_roles is a dependency FACTORY meant to be used via
`Depends(require_roles(*roles))` — called like this it just builds and
discards a dependency callable, enforcing nothing), referenced
`current_user.student_id` (User has no such field) and `Class.academic_term_id`
(classes aren't term-scoped in this schema). None of it had ever been
exercised against real data.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional
import json

from database import get_session
from auth import get_current_user, require_roles
from models.user import User, UserRole
from models.analytics import AnalyticsSnapshot, ClassPerformanceSummary
from models.student import Student
from models.classroom import Class
from models.school import AcademicTerm
from services.analytics import AnalyticsService

router = APIRouter(prefix="/analytics", tags=["Analytics"])

STAFF_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR, UserRole.TEACHER)
COMPARISON_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR)


def _school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


async def _current_term_id(session: AsyncSession, school_id: str) -> str:
    term = (await session.execute(
        select(AcademicTerm).where(AcademicTerm.school_id == school_id, AcademicTerm.is_current == True)  # noqa: E712
    )).scalar_one_or_none()
    if not term:
        raise HTTPException(status_code=400, detail="No active academic term")
    return term.id


@router.get("/student/{student_id}")
async def get_student_analytics(
    student_id: str,
    academic_term_id: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Comprehensive analytics for one student — admin/HR/teachers, or the
    student themself (matched via Student.user_id, same pattern used
    throughout the app rather than a nonexistent User.student_id field)."""
    school_id = _school_id(current_user)
    student = await session.get(Student, student_id)
    if not student or student.school_id != school_id:
        raise HTTPException(status_code=404, detail="Student not found")

    if current_user.role not in STAFF_ROLES:
        if current_user.role != UserRole.STUDENT or student.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Not authorized to view this student's analytics")

    if not academic_term_id:
        academic_term_id = await _current_term_id(session, school_id)

    snapshot = (await session.execute(
        select(AnalyticsSnapshot).where(
            AnalyticsSnapshot.student_id == student_id, AnalyticsSnapshot.academic_term_id == academic_term_id, AnalyticsSnapshot.school_id == school_id,
        ).order_by(AnalyticsSnapshot.captured_at.desc()).limit(1)
    )).scalar_one_or_none()

    if not snapshot:
        snapshot = await AnalyticsService.calculate_student_snapshot(session, student_id, school_id, academic_term_id)
        await session.commit()
        await session.refresh(snapshot)

    risk_factors = json.loads(snapshot.risk_factors) if snapshot.risk_factors else []
    return {
        "student_id": student.student_id,
        "student_name": f"{student.first_name} {student.last_name}",
        "overall_gpa": snapshot.overall_gpa,
        "attendance_rate": snapshot.attendance_rate,
        "assignment_completion_rate": snapshot.assignment_completion_rate,
        "best_subject": snapshot.best_subject,
        "best_subject_grade": snapshot.best_subject_grade,
        "worst_subject": snapshot.worst_subject,
        "worst_subject_grade": snapshot.worst_subject_grade,
        "gpa_trend": snapshot.gpa_trend,
        "attendance_trend": snapshot.attendance_trend,
        "risk_level": snapshot.risk_level,
        "risk_factors": risk_factors,
        "captured_at": snapshot.captured_at.isoformat(),
    }


@router.get("/class/{class_id}")
async def get_class_analytics(
    class_id: str,
    academic_term_id: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*STAFF_ROLES)),
) -> dict:
    school_id = _school_id(current_user)
    classroom = await session.get(Class, class_id)
    if not classroom or classroom.school_id != school_id:
        raise HTTPException(status_code=404, detail="Class not found")

    if not academic_term_id:
        academic_term_id = await _current_term_id(session, school_id)

    summary = (await session.execute(
        select(ClassPerformanceSummary).where(
            ClassPerformanceSummary.class_id == class_id, ClassPerformanceSummary.academic_term_id == academic_term_id, ClassPerformanceSummary.school_id == school_id,
        ).order_by(ClassPerformanceSummary.last_updated.desc()).limit(1)
    )).scalar_one_or_none()

    if not summary:
        summary = await AnalyticsService.calculate_class_summary(session, class_id, school_id, academic_term_id)
        await session.commit()
        await session.refresh(summary)

    return {
        "class_id": classroom.id, "class_name": classroom.name, "total_students": summary.total_students,
        "average_gpa": summary.average_gpa, "average_attendance_rate": summary.average_attendance_rate,
        "students_at_risk": summary.students_at_risk, "students_passing": summary.students_passing, "students_failing": summary.students_failing,
        "strongest_subject": summary.strongest_subject, "weakest_subject": summary.weakest_subject,
        "gpa_trend": summary.gpa_trend, "at_risk_trend": summary.at_risk_trend,
        "last_updated": summary.last_updated.isoformat(),
    }


@router.get("/at-risk")
async def list_at_risk_students(
    academic_term_id: Optional[str] = None,
    limit: int = Query(20, le=100),
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*STAFF_ROLES)),
) -> dict:
    school_id = _school_id(current_user)
    if not academic_term_id:
        academic_term_id = await _current_term_id(session, school_id)
    at_risk = await AnalyticsService.get_at_risk_students(session, school_id, academic_term_id, limit)
    return {"count": len(at_risk), "students": at_risk, "academic_term_id": academic_term_id}


@router.get("/class-comparison")
async def compare_class_performance(
    academic_term_id: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*COMPARISON_ROLES)),
) -> dict:
    school_id = _school_id(current_user)
    if not academic_term_id:
        academic_term_id = await _current_term_id(session, school_id)
    classes = await AnalyticsService.get_class_performance_comparison(session, school_id, academic_term_id)
    return {"count": len(classes), "classes": classes, "academic_term_id": academic_term_id}


@router.post("/calculate-all")
async def trigger_analytics_calculation(
    academic_term_id: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
) -> dict:
    """Recalculates every active student's snapshot and every class's
    summary for a term — run at end of term or on-demand from the dashboard."""
    school_id = _school_id(current_user)
    if not academic_term_id:
        academic_term_id = await _current_term_id(session, school_id)

    students = (await session.execute(
        select(Student).where(Student.school_id == school_id, Student.status == "active")
    )).scalars().all()

    count = 0
    for student in students:
        try:
            await AnalyticsService.calculate_student_snapshot(session, student.id, school_id, academic_term_id)
            count += 1
        except Exception as e:
            logger_msg = f"Error calculating analytics for student {student.id}: {e}"
            print(logger_msg)
    await session.commit()

    class_ids = (await session.execute(
        select(Student.class_id).where(Student.school_id == school_id, Student.status == "active", Student.class_id.is_not(None)).distinct()
    )).scalars().all()

    class_count = 0
    for class_id in class_ids:
        try:
            await AnalyticsService.calculate_class_summary(session, class_id, school_id, academic_term_id)
            class_count += 1
        except Exception as e:
            print(f"Error calculating class analytics for class {class_id}: {e}")
    await session.commit()

    return {"status": "success", "students_calculated": count, "classes_calculated": class_count, "academic_term_id": academic_term_id}
