"""Proactive attendance-risk alerting — the automated counterpart to
routers/sms.py::send_attendance_alert_sms (a manual tool a staff member
triggers with a percentage they supply by hand). Mirrors
services/fee_reminder_service.py's shape: per-school settings, a
cooldown-based dedupe log, SMS+email to every linked parent."""
import logging
from datetime import date, datetime, timedelta
from typing import Dict, Optional

from sqlmodel import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from database import async_session
from models.attendance import Attendance, AttendanceStatus
from models.attendance_risk import AttendanceRiskSettings, AttendanceRiskAlert
from models.student import Student, StudentParent, Parent
from services.sms_service import sms_service
from services.email_service import email_service

logger = logging.getLogger(__name__)


async def get_or_create_settings(session: AsyncSession, school_id: str) -> AttendanceRiskSettings:
    result = await session.execute(select(AttendanceRiskSettings).where(AttendanceRiskSettings.school_id == school_id))
    settings = result.scalar_one_or_none()
    if not settings:
        settings = AttendanceRiskSettings(school_id=school_id)
        session.add(settings)
        await session.flush()
    return settings


async def _attendance_rate(session: AsyncSession, student_id: str, school_id: str, since: str) -> Optional[float]:
    """PRESENT+LATE counts as "present" — matches every other attendance-rate
    consumer in this codebase (report cards, teacher/student/parent portals,
    strategic reports): a late arrival is still physical presence, whereas
    an EXCUSED absence is still an absence (just one that isn't held against
    the student for other purposes). Previously counted PRESENT+EXCUSED
    here, which under-flagged students with many excused absences — the
    exact population a truancy/risk sweep exists to catch."""
    total = (await session.execute(
        select(func.count(Attendance.id)).where(
            Attendance.student_id == student_id, Attendance.school_id == school_id, Attendance.attendance_date >= since,
        )
    )).scalar() or 0
    if total == 0:
        return None
    present = (await session.execute(
        select(func.count(Attendance.id)).where(
            Attendance.student_id == student_id, Attendance.school_id == school_id, Attendance.attendance_date >= since,
            Attendance.status.in_([AttendanceStatus.PRESENT, AttendanceStatus.LATE]),
        )
    )).scalar() or 0
    return present / total * 100


def _message(student_name: str, rate: float, threshold: float) -> str:
    return (
        f"Attendance Alert: {student_name}'s attendance rate is {rate:.1f}%, "
        f"below the school's {threshold:.0f}% threshold. Please contact the school if you have concerns."
    )


async def _alert_for_student(session: AsyncSession, school_id: str, student: Student, rate: float, settings: AttendanceRiskSettings) -> int:
    cutoff = datetime.utcnow() - timedelta(days=settings.cooldown_days)
    recent = (await session.execute(
        select(AttendanceRiskAlert).where(
            AttendanceRiskAlert.student_id == student.id, AttendanceRiskAlert.school_id == school_id,
            AttendanceRiskAlert.sent == True, AttendanceRiskAlert.created_at >= cutoff,  # noqa: E712
        )
    )).scalar_one_or_none()
    if recent:
        return 0

    parents = (await session.execute(
        select(Parent).join(StudentParent, StudentParent.parent_id == Parent.id).where(StudentParent.student_id == student.id)
    )).scalars().all()
    if not parents:
        return 0

    student_name = f"{student.first_name} {student.last_name}"
    message = _message(student_name, rate, settings.threshold_percent)
    sent_count = 0

    for parent in parents:
        if parent.phone:
            sms_result = await sms_service.send_sms([parent.phone], message)
            session.add(AttendanceRiskAlert(
                school_id=school_id, student_id=student.id, attendance_rate=rate, channel="sms",
                message=message, recipient=parent.phone,
                sent=bool(sms_result.get("success")), sent_at=datetime.utcnow() if sms_result.get("success") else None,
                status="sent" if sms_result.get("success") else "failed",
                error_message=None if sms_result.get("success") else sms_result.get("error"),
            ))
            if sms_result.get("success"):
                sent_count += 1
        if parent.email:
            email_result = await email_service.send_email([parent.email], "Attendance Risk Alert", message)
            session.add(AttendanceRiskAlert(
                school_id=school_id, student_id=student.id, attendance_rate=rate, channel="email",
                message=message, recipient=parent.email,
                sent=bool(email_result.get("success")), sent_at=datetime.utcnow() if email_result.get("success") else None,
                status="sent" if email_result.get("success") else "failed",
                error_message=None if email_result.get("success") else email_result.get("error"),
            ))
            if email_result.get("success"):
                sent_count += 1

    return sent_count


async def run_risk_sweep(session: AsyncSession, school_id: Optional[str] = None) -> Dict:
    settings_query = select(AttendanceRiskSettings)
    if school_id:
        settings_query = settings_query.where(AttendanceRiskSettings.school_id == school_id)
    settings_rows = (await session.execute(settings_query)).scalars().all()

    school_ids = {school_id} if school_id else set()
    if not school_id:
        all_school_ids = (await session.execute(select(Student.school_id).distinct())).scalars().all()
        school_ids.update(all_school_ids)

    settings_by_school = {s.school_id: s for s in settings_rows}
    students_checked = 0
    total_sent = 0
    students_flagged = 0

    for sid in school_ids:
        settings = settings_by_school.get(sid) or await get_or_create_settings(session, sid)
        if not settings.enabled:
            continue

        since = (date.today() - timedelta(days=settings.lookback_days)).isoformat()
        students = (await session.execute(
            select(Student).where(Student.school_id == sid, Student.status == "active")
        )).scalars().all()

        for student in students:
            students_checked += 1
            rate = await _attendance_rate(session, student.id, sid, since)
            if rate is None or rate >= settings.threshold_percent:
                continue
            students_flagged += 1
            total_sent += await _alert_for_student(session, sid, student, rate, settings)

    await session.commit()
    return {"students_checked": students_checked, "students_flagged": students_flagged, "alerts_sent": total_sent}


async def run_attendance_risk_sweep() -> None:
    """Called by services/scheduler.py — every school, every day."""
    async with async_session() as session:
        result = await run_risk_sweep(session, school_id=None)
        logger.info(
            f"Attendance risk sweep: {result['students_flagged']} student(s) flagged below threshold, "
            f"{result['alerts_sent']} alert(s) sent across {result['students_checked']} student(s) checked"
        )
