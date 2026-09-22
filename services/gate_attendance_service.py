"""Gate/entrance attendance business logic: recording a student's arrival
and departure timestamps for the day and flagging lateness against a
school's configured cutoff times (models.gate_attendance.GateAttendanceSettings).

Time comparison note: like the rest of this codebase, there's no per-school
timezone field anywhere — every timestamp is naive datetime.utcnow(). Cutoff
times ("HH:MM") are compared directly against the server clock's current
time, so a school's cutoff should be entered in whatever timezone the
deployment's server clock actually runs in (consistent with how every other
time-of-day field in this codebase — e.g. models.shift.Shift — already
works, not a new gap introduced here).
"""
import logging
from datetime import datetime
from typing import Optional, Tuple

from sqlmodel import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.attendance import Attendance, AttendanceStatus
from models.gate_attendance import GateAttendance, GateAttendanceSettings
from models.security import StudentSecurityProfile
from models.student import Student, Parent, StudentParent
from models.communication import MessageType
from services import parent_notification_service, security_alert_service
from services.sms_service import sms_service
from routers.timetable import get_current_term_id

logger = logging.getLogger(__name__)

# ADMS devices commonly re-send an unacknowledged event on retry (e.g. a
# network blip right after the device's first attempt). Without a debounce,
# that resend reads as a second, distinct scan and gets recorded as a
# check-out for a student who never left — corrupting the "who's on campus"
# view and firing a false "picked up" SMS to the parent. A genuine same-day
# out-and-back is real but rare enough, and slow enough (minutes, not
# seconds), that this window doesn't get in its way.
GATE_PUNCH_DEBOUNCE_SECONDS = 90


def today_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


async def get_settings(session: AsyncSession, school_id: str) -> GateAttendanceSettings:
    result = await session.execute(
        select(GateAttendanceSettings).where(GateAttendanceSettings.school_id == school_id)
    )
    settings = result.scalar_one_or_none()
    if not settings:
        settings = GateAttendanceSettings(school_id=school_id)
        session.add(settings)
        await session.commit()
        await session.refresh(settings)
    return settings


async def _get_or_create_today_row(session: AsyncSession, school_id: str, student_id: str) -> GateAttendance:
    date_str = today_str()
    result = await session.execute(
        select(GateAttendance).where(
            GateAttendance.school_id == school_id,
            GateAttendance.student_id == student_id,
            GateAttendance.date == date_str,
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        row = GateAttendance(school_id=school_id, student_id=student_id, date=date_str)
        session.add(row)
    return row


def _is_past_cutoff(cutoff: Optional[str]) -> bool:
    if not cutoff:
        return False
    return datetime.utcnow().strftime("%H:%M") > cutoff


async def record_check_in(
    session: AsyncSession, school_id: str, student_id: str, recorded_by: str, notes: Optional[str] = None,
    punched_at: Optional[datetime] = None, method: str = "gate",
) -> Tuple[GateAttendance, bool]:
    """Logs (or overwrites, if called again the same day) this student's
    gate check-in time. Returns (row, is_late). punched_at overrides the
    server-receipt time with a device's own reported scan time (used by
    routers/biometric_adms.py) — omitted by the manual gate-desk endpoint,
    which has no such value and wants server time. method mirrors
    record_check_out's ("gate" | "qr_id_card" | "biometric")."""
    settings = await get_settings(session, school_id)
    row = await _get_or_create_today_row(session, school_id, student_id)

    row.check_in_time = punched_at or datetime.utcnow()
    row.check_in_by = recorded_by
    row.check_in_method = method
    row.is_late = _is_past_cutoff(settings.late_cutoff_time)
    if notes:
        row.notes = notes
    row.updated_at = datetime.utcnow()

    session.add(row)
    await session.commit()
    await session.refresh(row)

    if settings.auto_mark_classroom_attendance:
        await _auto_mark_classroom_attendance(session, school_id, student_id, row)

    return row, row.is_late


async def _auto_mark_classroom_attendance(
    session: AsyncSession, school_id: str, student_id: str, gate_row: GateAttendance,
) -> None:
    """Opt-in bridge to classroom roll-call attendance (models.attendance.Attendance)
    — only runs when a school has turned on GateAttendanceSettings.auto_mark_
    classroom_attendance. Creates today's Attendance row as PRESENT if (and
    only if) none exists yet; a teacher's own mark always wins and is never
    touched here. Tagged recorded_by=f"gate_attendance:{gate_row.id}" so
    routers/attendance.py can tell an auto-created row apart from a real
    teacher/admin one (see _is_auto_sourced there) and safely overwrite it
    later, and so it's traceable back to the gate scan that created it.

    Never raises: a hiccup here must never block the gate check-in itself,
    mirroring services.parent_notification_service's "log and swallow"
    contract for the same reason."""
    try:
        student = await session.get(Student, student_id)
        if not student or not student.class_id:
            return

        existing = await session.execute(
            select(Attendance).where(
                Attendance.school_id == school_id,
                Attendance.student_id == student_id,
                Attendance.attendance_date == gate_row.date,
            )
        )
        if existing.scalar_one_or_none():
            return

        academic_term_id = await get_current_term_id(session, school_id)
        if not academic_term_id:
            return

        attendance = Attendance(
            school_id=school_id, student_id=student_id, class_id=student.class_id,
            academic_term_id=academic_term_id, attendance_date=gate_row.date,
            status=AttendanceStatus.PRESENT, recorded_by=f"gate_attendance:{gate_row.id}",
        )
        session.add(attendance)
        await session.commit()
    except IntegrityError:
        await session.rollback()
    except Exception:
        await session.rollback()
        logger.exception(f"[gate_attendance] auto-mark classroom attendance failed for student {student_id}")


async def record_check_out(
    session: AsyncSession, school_id: str, student_id: str, recorded_by: Optional[str],
    method: str = "gate", notes: Optional[str] = None, punched_at: Optional[datetime] = None,
) -> Tuple[GateAttendance, bool]:
    """Logs this student's gate check-out/pickup time — called either from
    the standalone gate check-out endpoint (method='gate') or from
    routers/security.py's QR pickup-completion flow (method='qr_pickup').
    Returns (row, is_late_pickup). See record_check_in on punched_at."""
    settings = await get_settings(session, school_id)
    row = await _get_or_create_today_row(session, school_id, student_id)

    row.check_out_time = punched_at or datetime.utcnow()
    row.check_out_by = recorded_by
    row.check_out_method = method
    row.is_late_pickup = _is_past_cutoff(settings.pickup_late_cutoff_time)
    if notes:
        row.notes = notes
    row.updated_at = datetime.utcnow()

    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row, row.is_late_pickup


async def record_gate_punch(
    session: AsyncSession, background_tasks, school_id: str, student, device_serial: str,
    punched_at: Optional[datetime] = None,
) -> dict:
    """A fingerprint/face-scan device reporting a student's gate scan —
    routers/biometric_adms.py's raw ADMS bridge, resolved via
    Student.biometric_device_pin. Auto-toggles check-in vs check-out the
    same way routers/attendance.py's record_staff_punch does for staff: no
    check-in yet today is a check-in, a check-in with no check-out is a
    check-out, a third scan the same day is a no-op — a device reports a
    raw scan, not a declared direction.

    A held student (StudentSecurityProfile.release_hold) cannot be checked
    out this way either — the same guard routers/gate_attendance.py's
    manual check-out endpoint applies, so a raw device scan isn't a way to
    bypass a hold that's enforced everywhere else. Check-in is unaffected
    by a hold (a hold blocks *release*, not arrival).
    """
    recorded_by = f"biometric_device:{device_serial}"
    row = await _get_or_create_today_row(session, school_id, student.id)
    event_time = punched_at or datetime.utcnow()

    if not row.check_in_time:
        direction = "check_in"
    elif not row.check_out_time:
        if abs((event_time - row.check_in_time).total_seconds()) < GATE_PUNCH_DEBOUNCE_SECONDS:
            return {
                "student_id": student.id, "direction": None,
                "message": "Duplicate scan ignored (received moments after check-in)",
            }
        direction = "check_out"
    elif (event_time - row.check_out_time).total_seconds() > GATE_PUNCH_DEBOUNCE_SECONDS:
        # A genuine same-day out-and-back (e.g. a medical appointment, then
        # back for the afternoon) — previously a 3rd+ scan was a silent
        # no-op. GateAttendance is one row per student per day, so this
        # starts a new cycle on the SAME row rather than a second row;
        # the prior check-out time is preserved in notes before being
        # overwritten, since it would otherwise be lost.
        prior_note = f"[Out-and-back: earlier check-out was {row.check_out_time.strftime('%H:%M')}]"
        row.notes = f"{row.notes} {prior_note}".strip() if row.notes else prior_note
        row.check_out_time = None
        direction = "check_in"
    else:
        return {"student_id": student.id, "direction": None, "message": "Already fully punched for today"}

    if direction == "check_out":
        profile = (await session.execute(
            select(StudentSecurityProfile).where(StudentSecurityProfile.student_id == student.id)
        )).scalar_one_or_none()
        if profile and profile.release_hold:
            return {"student_id": student.id, "direction": None, "message": "Release hold active — check-out skipped"}

        row, is_late_pickup = await record_check_out(
            session, school_id, student.id, recorded_by=recorded_by, method="biometric", punched_at=punched_at,
        )
        time_label = row.check_out_time.strftime("%H:%M")
        await parent_notification_service.notify_parent(
            session, student, None, background_tasks,
            sms_message=(
                f"{student.first_name} {student.last_name} was picked up from school at {time_label}"
                + (" (late pickup)." if is_late_pickup else ".")
            ),
            in_app_subject="School Check-Out" + (" (Late Pickup)" if is_late_pickup else ""),
            in_app_content=f"{student.first_name} {student.last_name} left the school gate at {time_label}"
                            + (" — this was after the school's pickup time." if is_late_pickup else "."),
            notification_type="gate_check_out",
            message_type=MessageType.ATTENDANCE,
        )
        return {"student_id": student.id, "direction": "check_out", "is_late_pickup": is_late_pickup}

    row, is_late = await record_check_in(
        session, school_id, student.id, recorded_by=recorded_by, punched_at=punched_at, method="biometric",
    )
    time_label = row.check_in_time.strftime("%H:%M")
    await parent_notification_service.notify_parent(
        session, student, None, background_tasks,
        sms_message=(
            f"{student.first_name} {student.last_name} checked in at school at {time_label}"
            + (" (late)." if is_late else ".")
        ),
        in_app_subject="School Check-In" + (" (Late)" if is_late else ""),
        in_app_content=f"{student.first_name} {student.last_name} checked in at the school gate at {time_label}"
                        + (" — this was after the school's start time." if is_late else "."),
        notification_type="gate_check_in",
        message_type=MessageType.ATTENDANCE,
    )
    return {"student_id": student.id, "direction": "check_in", "is_late": is_late}


async def _notify_all_parents_no_checkout(session: AsyncSession, student: Student, message: str) -> None:
    """Direct SMS to every linked parent — this runs from a standalone
    scheduler job (services/scheduler.py), not an HTTP request, so it
    can't rely on parent_notification_service.notify_parent's
    BackgroundTasks.add_task for the SMS leg (nothing would ever execute
    a BackgroundTasks instance that FastAPI itself never dispatches).
    Mirrors services/attendance_risk_service.py's direct-await SMS shape,
    which runs from the same scheduler."""
    parents_result = await session.execute(
        select(Parent).join(StudentParent, StudentParent.parent_id == Parent.id)
        .where(StudentParent.student_id == student.id)
    )
    for parent in parents_result.scalars().all():
        if not parent.phone or not sms_service.validate_phone_number(parent.phone):
            continue
        try:
            await sms_service.send_sms([sms_service.format_phone_number(parent.phone)], message)
        except Exception:
            logger.exception(f"Failed to send no-checkout escalation SMS for student {student.id}")


async def run_gate_pickup_escalation_sweep() -> dict:
    """Daily sweep: flags every student who checked in at the gate today but
    was never checked out by the time this runs — previously invisible
    unless an admin happened to open routers/gate_attendance.py's "still on
    campus" view themselves. Escalates once per row (escalated_at guards
    against re-alerting on a same-day misfire retry) via SMS to every
    linked parent AND a persisted, must-be-acknowledged SecurityIncident +
    SMS to the school's own admin/security staff (services.security_alert_service).
    Called by services/scheduler.py, once daily, well after typical
    dismissal — see that module for the exact hour and rationale."""
    from database import async_session

    escalated = 0
    async with async_session() as session:
        today = today_str()
        result = await session.execute(
            select(GateAttendance).where(
                GateAttendance.date == today,
                GateAttendance.check_in_time.is_not(None),
                GateAttendance.check_out_time.is_(None),
                GateAttendance.escalated_at.is_(None),
            )
        )
        rows = result.scalars().all()
        for row in rows:
            student = await session.get(Student, row.student_id)
            if not student:
                continue
            student_name = f"{student.first_name} {student.last_name}"
            check_in_label = row.check_in_time.strftime("%H:%M")

            await _notify_all_parents_no_checkout(
                session, student,
                f"{student_name} checked in at school at {check_in_label} today and has not yet been "
                f"checked out. Please confirm they have been safely collected, or contact the school.",
            )
            await security_alert_service.raise_incident(
                session, row.school_id, "late_pickup_unresolved",
                sms_message=f"Security alert: {student_name} checked in at {check_in_label} and has not been checked out today.",
                student_id=student.id,
                details=f"Gate check-in at {check_in_label}, no check-out recorded as of the escalation sweep.",
            )

            row.escalated_at = datetime.utcnow()
            session.add(row)
            escalated += 1

        if rows:
            await session.commit()

    return {"escalated": escalated}
