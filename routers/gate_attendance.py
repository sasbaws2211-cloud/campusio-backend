"""Gate/entrance attendance router — a security-desk arrival/departure log,
distinct from classroom roll-call attendance (routers/attendance.py) and
from the QR pickup-dispatch system (routers/security.py). See
models/gate_attendance.py's module docstring for how the three relate.
"""
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user, require_roles
from database import get_session
from models.gate_attendance import (
    GateAttendance, GateAttendanceSettings, GateCheckInRequest, GateCheckOutRequest,
    GateScanRequest, UpdateGateAttendanceSettingsRequest,
)
from models.student import Student
from models.security import StudentSecurityProfile
from models.certificates import IDCard, IDCardStatus, PersonType
from models.user import User, UserRole
from services import gate_attendance_service, parent_notification_service
from models.communication import MessageType
from services.plan_gating import require_plan_feature

router = APIRouter(
    prefix="/gate-attendance", tags=["Gate Attendance"],
    dependencies=[Depends(require_plan_feature("gate_attendance"))],
)

GATE_STAFF_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.SECURITY_OFFICER, UserRole.REGISTRAR)


def _school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


async def _get_active_student(session: AsyncSession, school_id: str, student_id: str) -> Student:
    student = await session.get(Student, student_id)
    if not student or student.school_id != school_id:
        raise HTTPException(status_code=404, detail="Student not found")
    return student


async def _resolve_student_by_card(session: AsyncSession, school_id: str, card_number: str) -> Student:
    """Resolves a scanned ID card to the student it belongs to — same
    lookup shape as routers/id_cards.py's GET /verify/{card_number}, but
    this caller needs the Student itself (to check-in/out), not just a
    display summary."""
    result = await session.execute(
        select(IDCard).where(IDCard.card_number == card_number, IDCard.school_id == school_id)
    )
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    if card.status != IDCardStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="This card is not active")
    if card.person_type != PersonType.STUDENT:
        raise HTTPException(status_code=400, detail="This card does not belong to a student")
    return await _get_active_student(session, school_id, card.person_id)


async def _block_if_release_hold(session: AsyncSession, student_id: str) -> None:
    """Campus-exit hold (routers/security.py) applies to every check-out
    path — manual, QR-card-scan, and biometric — so none of them can be a
    way to bypass a hold that's meant to be enforced everywhere."""
    profile = (await session.execute(
        select(StudentSecurityProfile).where(StudentSecurityProfile.student_id == student_id)
    )).scalar_one_or_none()
    if profile and profile.release_hold:
        raise HTTPException(
            status_code=403,
            detail=f"This student has an active release hold and cannot be released. {profile.release_hold_reason or 'Contact school administration.'}",
        )


async def _notify_gate_event(
    session: AsyncSession, student: Student, current_user: User, background_tasks: BackgroundTasks,
    row: GateAttendance, is_late: bool, direction: str,
) -> None:
    """Shared parent-notification call for every check-in/check-out
    endpoint below (manual + QR-card-scan) — was inlined near-identically
    in each one; extracted once a third and fourth caller needed it."""
    if direction == "check_in":
        time_label = row.check_in_time.strftime("%H:%M")
        await parent_notification_service.notify_parent(
            session, student, current_user, background_tasks,
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
    else:
        time_label = row.check_out_time.strftime("%H:%M")
        await parent_notification_service.notify_parent(
            session, student, current_user, background_tasks,
            sms_message=(
                f"{student.first_name} {student.last_name} was picked up from school at {time_label}"
                + (" (late pickup)." if is_late else ".")
            ),
            in_app_subject="School Check-Out" + (" (Late Pickup)" if is_late else ""),
            in_app_content=f"{student.first_name} {student.last_name} left the school gate at {time_label}"
                            + (" — this was after the school's pickup time." if is_late else "."),
            notification_type="gate_check_out",
            message_type=MessageType.ATTENDANCE,
        )


def _row_dict(row: GateAttendance, student: Optional[Student] = None) -> dict:
    return {
        "id": row.id,
        "student_id": row.student_id,
        "student_name": f"{student.first_name} {student.last_name}" if student else None,
        "class_id": student.class_id if student else None,
        "date": row.date,
        "check_in_time": row.check_in_time.isoformat() if row.check_in_time else None,
        "check_in_method": row.check_in_method,
        "is_late": row.is_late,
        "check_out_time": row.check_out_time.isoformat() if row.check_out_time else None,
        "check_out_method": row.check_out_method,
        "is_late_pickup": row.is_late_pickup,
        "notes": row.notes,
    }


@router.post("/check-in", response_model=dict)
async def check_in_student(
    body: GateCheckInRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_roles(*GATE_STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """Log a student's arrival at the gate. Safe to call more than once for
    the same student/day — each call overwrites the check-in time (e.g. a
    correction), it does not create duplicate rows."""
    school_id = _school_id(current_user)
    student = await _get_active_student(session, school_id, body.student_id)

    row, is_late = await gate_attendance_service.record_check_in(
        session, school_id, body.student_id, recorded_by=current_user.id, notes=body.notes,
    )
    await _notify_gate_event(session, student, current_user, background_tasks, row, is_late, "check_in")

    return {**_row_dict(row, student), "message": "Check-in recorded" + (" (late)" if is_late else "")}


@router.post("/check-in/scan", response_model=dict)
async def check_in_student_scan(
    body: GateScanRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_roles(*GATE_STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """Staff scans a student's ID card QR (IDCard.card_number — the same
    static value already printed/encoded on the card, see
    routers/id_cards.py's verify endpoint) to log their arrival."""
    school_id = _school_id(current_user)
    student = await _resolve_student_by_card(session, school_id, body.card_number)

    row, is_late = await gate_attendance_service.record_check_in(
        session, school_id, student.id, recorded_by=current_user.id, notes=body.notes, method="qr_id_card",
    )
    await _notify_gate_event(session, student, current_user, background_tasks, row, is_late, "check_in")

    return {**_row_dict(row, student), "message": "Check-in recorded" + (" (late)" if is_late else "")}


@router.post("/check-out", response_model=dict)
async def check_out_student(
    body: GateCheckOutRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_roles(*GATE_STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """Log a student's departure from the gate — for schools not using the
    QR pickup flow (routers/security.py), or as a manual override/correction
    even when they are."""
    school_id = _school_id(current_user)
    student = await _get_active_student(session, school_id, body.student_id)
    await _block_if_release_hold(session, body.student_id)

    row, is_late_pickup = await gate_attendance_service.record_check_out(
        session, school_id, body.student_id, recorded_by=current_user.id, method="gate", notes=body.notes,
    )
    await _notify_gate_event(session, student, current_user, background_tasks, row, is_late_pickup, "check_out")

    return {**_row_dict(row, student), "message": "Check-out recorded" + (" (late pickup)" if is_late_pickup else "")}


@router.post("/check-out/scan", response_model=dict)
async def check_out_student_scan(
    body: GateScanRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_roles(*GATE_STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """Staff scans a student's ID card QR to log their departure — an
    alternative to the parent pickup-dispatch QR flow (routers/security.py)
    for a student who leaves without a collector (e.g. walks/bikes home)."""
    school_id = _school_id(current_user)
    student = await _resolve_student_by_card(session, school_id, body.card_number)
    await _block_if_release_hold(session, student.id)

    row, is_late_pickup = await gate_attendance_service.record_check_out(
        session, school_id, student.id, recorded_by=current_user.id, method="qr_id_card", notes=body.notes,
    )
    await _notify_gate_event(session, student, current_user, background_tasks, row, is_late_pickup, "check_out")

    return {**_row_dict(row, student), "message": "Check-out recorded" + (" (late pickup)" if is_late_pickup else "")}


@router.get("/today", response_model=List[dict])
async def get_today_gate_attendance(
    class_id: Optional[str] = None,
    current_user: User = Depends(require_roles(*GATE_STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """Every active student in the school (or one class), with today's
    check-in/out status if any — lets gate staff see at a glance who hasn't
    arrived yet, and admins see who's still on campus."""
    school_id = _school_id(current_user)

    student_query = select(Student).where(Student.school_id == school_id, Student.status == "active")
    if class_id:
        student_query = student_query.where(Student.class_id == class_id)
    students = (await session.execute(student_query)).scalars().all()
    if not students:
        return []

    date_str = gate_attendance_service.today_str()
    rows = (await session.execute(
        select(GateAttendance).where(
            GateAttendance.school_id == school_id,
            GateAttendance.date == date_str,
            GateAttendance.student_id.in_([s.id for s in students]),
        )
    )).scalars().all()
    rows_by_student = {r.student_id: r for r in rows}

    result = []
    for student in students:
        row = rows_by_student.get(student.id)
        if row:
            result.append(_row_dict(row, student))
        else:
            result.append({
                "id": None, "student_id": student.id,
                "student_name": f"{student.first_name} {student.last_name}",
                "class_id": student.class_id, "date": date_str,
                "check_in_time": None, "is_late": False,
                "check_out_time": None, "check_out_method": None, "is_late_pickup": False,
                "notes": None,
            })
    result.sort(key=lambda r: r["student_name"] or "")
    return result


def _date_range_params(start_date: Optional[str], end_date: Optional[str]) -> tuple:
    if not end_date:
        end_date = gate_attendance_service.today_str()
    if not start_date:
        start_date = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
    return start_date, end_date


@router.get("/late-arrivals", response_model=List[dict])
async def list_late_arrivals(
    start_date: Optional[str] = Query(None, description="YYYY-MM-DD, defaults to 30 days ago"),
    end_date: Optional[str] = Query(None, description="YYYY-MM-DD, defaults to today"),
    class_id: Optional[str] = None,
    current_user: User = Depends(require_roles(*GATE_STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """Every late check-in in the date range, most recent first."""
    school_id = _school_id(current_user)
    start_date, end_date = _date_range_params(start_date, end_date)

    query = select(GateAttendance, Student).join(Student, Student.id == GateAttendance.student_id).where(
        GateAttendance.school_id == school_id,
        GateAttendance.is_late == True,
        GateAttendance.date >= start_date,
        GateAttendance.date <= end_date,
    )
    if class_id:
        query = query.where(Student.class_id == class_id)
    rows = (await session.execute(query.order_by(GateAttendance.check_in_time.desc()))).all()
    return [_row_dict(r, s) for r, s in rows]


@router.get("/late-pickups", response_model=List[dict])
async def list_late_pickups(
    start_date: Optional[str] = Query(None, description="YYYY-MM-DD, defaults to 30 days ago"),
    end_date: Optional[str] = Query(None, description="YYYY-MM-DD, defaults to today"),
    class_id: Optional[str] = None,
    current_user: User = Depends(require_roles(*GATE_STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """Every late pickup in the date range, most recent first."""
    school_id = _school_id(current_user)
    start_date, end_date = _date_range_params(start_date, end_date)

    query = select(GateAttendance, Student).join(Student, Student.id == GateAttendance.student_id).where(
        GateAttendance.school_id == school_id,
        GateAttendance.is_late_pickup == True,
        GateAttendance.date >= start_date,
        GateAttendance.date <= end_date,
    )
    if class_id:
        query = query.where(Student.class_id == class_id)
    rows = (await session.execute(query.order_by(GateAttendance.check_out_time.desc()))).all()
    return [_row_dict(r, s) for r, s in rows]


@router.get("/daily-summary", response_model=dict)
async def get_daily_summary(
    date: Optional[str] = Query(None, description="YYYY-MM-DD, defaults to today"),
    current_user: User = Depends(require_roles(*GATE_STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """Gate-log-derived daily counts — distinct from routers/attendance.py's
    per-class present/absent/late/excused summary (that's roll-call based;
    this is purely what was scanned/logged at the gate)."""
    school_id = _school_id(current_user)
    date_str = date or gate_attendance_service.today_str()

    total_students = (await session.execute(
        select(Student).where(Student.school_id == school_id, Student.status == "active")
    )).scalars().all()
    total_count = len(total_students)

    rows = (await session.execute(
        select(GateAttendance).where(GateAttendance.school_id == school_id, GateAttendance.date == date_str)
    )).scalars().all()

    checked_in = sum(1 for r in rows if r.check_in_time)
    late_arrivals = sum(1 for r in rows if r.is_late)
    checked_out = sum(1 for r in rows if r.check_out_time)
    late_pickups = sum(1 for r in rows if r.is_late_pickup)

    return {
        "date": date_str,
        "total_students": total_count,
        "checked_in": checked_in,
        "not_checked_in": total_count - checked_in,
        "late_arrivals": late_arrivals,
        "checked_out": checked_out,
        "late_pickups": late_pickups,
    }


@router.get("/settings", response_model=dict)
async def get_gate_settings(
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    settings = await gate_attendance_service.get_settings(session, school_id)
    return {
        "late_cutoff_time": settings.late_cutoff_time,
        "pickup_late_cutoff_time": settings.pickup_late_cutoff_time,
        "auto_mark_classroom_attendance": settings.auto_mark_classroom_attendance,
    }


@router.put("/settings", response_model=dict)
async def update_gate_settings(
    body: UpdateGateAttendanceSettingsRequest,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    settings = await gate_attendance_service.get_settings(session, school_id)

    if body.late_cutoff_time is not None:
        settings.late_cutoff_time = body.late_cutoff_time or None
    if body.pickup_late_cutoff_time is not None:
        settings.pickup_late_cutoff_time = body.pickup_late_cutoff_time or None
    if body.auto_mark_classroom_attendance is not None:
        settings.auto_mark_classroom_attendance = body.auto_mark_classroom_attendance
    settings.updated_at = datetime.utcnow()

    session.add(settings)
    await session.commit()
    await session.refresh(settings)
    return {
        "late_cutoff_time": settings.late_cutoff_time,
        "pickup_late_cutoff_time": settings.pickup_late_cutoff_time,
        "auto_mark_classroom_attendance": settings.auto_mark_classroom_attendance,
    }
