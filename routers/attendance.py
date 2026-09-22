"""Attendance router"""
import ipaddress
import secrets
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status, Query, Request
from fastapi.responses import StreamingResponse
from sqlmodel import select, func, SQLModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from datetime import datetime, timedelta
from typing import Optional
from models.attendance import (
    Attendance, AttendanceCreate, AttendanceStatus, AttendanceBulkCreate,
    StaffAttendance, StaffAttendanceCreate, StaffAttendanceBulkCreate,
    StaffAttendanceSettings, StaffAttendanceSettingsUpdate, ClockInMode, ClockRequest,
)
from models.shift import Shift, ShiftCreate, ShiftUpdate, ShiftAssign, ShiftBulkAssign
from models.student import Student, StudentParent, Parent, StudentStatus
from models.classroom import Class
from models.staff import Staff, StaffStatus, TeacherAssignment, StaffShiftHistory
from models.school import AcademicTerm
from models.user import User, UserRole
from database import get_session
from dependencies import assert_campus_access, resolve_campus_scope
from auth import get_current_user, require_permission
from services.audit_service import log_event
from services.attendance_shared import is_working_day, fetch_holiday_dates
from services.attendance_export_service import generate_staff_attendance_csv
from services.plan_gating import require_plan_feature
from services.webhook_service import emit_event

router = APIRouter(prefix="/attendance", tags=["Attendance"])


def _is_auto_sourced(recorded_by: Optional[str]) -> bool:
    """True for an Attendance row a system wrote on a student's behalf
    (a gate check-in via services.gate_attendance_service, or a biometric
    device via the punch endpoint below) rather than a teacher/admin's own
    mark (recorded_by is that user's id). A teacher's POST/bulk-POST should
    freely overwrite an auto-sourced row — it's a placeholder, not a real
    record yet — but must never silently clobber another human's mark."""
    return bool(recorded_by) and (
        recorded_by.startswith("gate_attendance:") or recorded_by.startswith("biometric_device:")
    )


async def validate_attendance_context(
    session: AsyncSession, current_user: User, school_id: str, class_id: str, academic_term_id: str
) -> None:
    """Reject attendance being recorded against a class/term that doesn't exist for
    this school, and — for teachers — one they aren't actually assigned to teach.
    None of student_id/class_id had a foreign key before this, so a bad or
    cross-tenant id was previously accepted silently."""
    class_result = await session.execute(
        select(Class).where(Class.id == class_id, Class.school_id == school_id)
    )
    cls = class_result.scalar_one_or_none()
    if not cls:
        raise HTTPException(status_code=400, detail="class_id does not exist for this school")
    assert_campus_access(current_user, cls.campus_id)

    term_result = await session.execute(
        select(AcademicTerm).where(AcademicTerm.id == academic_term_id, AcademicTerm.school_id == school_id)
    )
    term = term_result.scalar_one_or_none()
    if not term:
        raise HTTPException(status_code=400, detail="academic_term_id does not exist for this school")
    if term.is_locked:
        raise HTTPException(status_code=423, detail="This academic term is locked and no longer accepts attendance records")

    if current_user.role == UserRole.TEACHER:
        staff_result = await session.execute(select(Staff).where(Staff.user_id == current_user.id))
        staff = staff_result.scalar_one_or_none()
        assignment = None
        if staff:
            assignment_result = await session.execute(
                select(TeacherAssignment).where(
                    TeacherAssignment.staff_id == staff.id,
                    TeacherAssignment.class_id == class_id
                )
            )
            # A teacher can have multiple assignments to the same class (one per
            # subject) — .first() rather than scalar_one_or_none(), which would
            # raise on a teacher who teaches this class more than one subject.
            assignment = assignment_result.scalars().first()
        if not assignment:
            raise HTTPException(status_code=403, detail="You are not assigned to teach this class")


async def validate_attendance_student(session: AsyncSession, school_id: str, student_id: str, class_id: str) -> None:
    """Reject an attendance record for a student who doesn't exist, isn't in this
    school, or isn't actually in the class attendance is being recorded for."""
    student_result = await session.execute(
        select(Student).where(Student.id == student_id, Student.school_id == school_id)
    )
    student = student_result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=400, detail=f"student_id {student_id} does not exist for this school")
    if student.class_id != class_id:
        raise HTTPException(status_code=400, detail=f"Student {student_id} is not in class {class_id}")

STAFF_ATTENDANCE_ADMIN_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR)


class UpdateAttendanceRequest(SQLModel):
    status: AttendanceStatus
    remarks: Optional[str] = None


class AutoAbsentRequest(SQLModel):
    attendance_date: Optional[str] = None  # defaults to yesterday



@router.post("", response_model=dict)
async def record_attendance(
    attendance_data: AttendanceCreate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_permission("attendance.mark.create")),
    session: AsyncSession = Depends(get_session)
):
    """Record attendance for a student"""
    school_id = current_user.school_id
    if not school_id and current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="No school context")

    await validate_attendance_context(session, current_user, school_id, attendance_data.class_id, attendance_data.academic_term_id)
    await validate_attendance_student(session, school_id, attendance_data.student_id, attendance_data.class_id)

    existing = await session.execute(
        select(Attendance).where(
            Attendance.school_id == school_id,
            Attendance.student_id == attendance_data.student_id,
            Attendance.attendance_date == attendance_data.attendance_date
        )
    )
    existing_row = existing.scalar_one_or_none()
    if existing_row and not _is_auto_sourced(existing_row.recorded_by):
        raise HTTPException(status_code=400, detail="Attendance already recorded for this date")

    if existing_row:
        # An auto-sourced placeholder (gate check-in / biometric) — a
        # teacher's own mark always wins, so update it in place rather
        # than reject as a duplicate.
        for field, value in attendance_data.model_dump().items():
            setattr(existing_row, field, value)
        existing_row.recorded_by = current_user.id
        attendance = existing_row
        session.add(attendance)
    else:
        attendance = Attendance(
            school_id=school_id,
            recorded_by=current_user.id,
            **attendance_data.model_dump()
        )
        session.add(attendance)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=400, detail="Attendance already recorded for this date")
    await session.refresh(attendance)

    from services.webhook_service import emit_event
    await emit_event(
        session, background_tasks, school_id, "attendance.marked",
        {
            "id": attendance.id,
            "student_id": attendance.student_id,
            "class_id": attendance.class_id,
            "date": attendance.attendance_date,
            "status": attendance.status,
        },
    )

    return {
        "id": attendance.id,
        "student_id": attendance.student_id,
        "date": attendance.attendance_date,
        "status": attendance.status,
        "message": "Attendance recorded"
    }


@router.post("/bulk", response_model=dict)
async def record_bulk_attendance(
    bulk_data: AttendanceBulkCreate,
    current_user: User = Depends(require_permission("attendance.mark.create")),
    session: AsyncSession = Depends(get_session)
):
    """Record attendance for multiple students"""
    school_id = current_user.school_id
    if not school_id and current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="No school context")
    
    # For super admin, school_id must come from bulk_data
    if current_user.role == UserRole.SUPER_ADMIN and not school_id:
        if not hasattr(bulk_data, 'school_id') or not bulk_data.school_id:
            raise HTTPException(status_code=400, detail="school_id required for super admin")
        school_id = bulk_data.school_id

    await validate_attendance_context(session, current_user, school_id, bulk_data.class_id, bulk_data.academic_term_id)

    # Batch-validate every record's student is real, in this school, and actually
    # in this class — a per-record query would be one extra query per student.
    submitted_student_ids = {record.get("student_id") for record in bulk_data.records}
    valid_students_result = await session.execute(
        select(Student.id).where(
            Student.id.in_(submitted_student_ids),
            Student.school_id == school_id,
            Student.class_id == bulk_data.class_id
        )
    )
    valid_student_ids = set(valid_students_result.scalars().all())
    if valid_student_ids != submitted_student_ids:
        raise HTTPException(
            status_code=400,
            detail="One or more students do not exist, aren't in this school, or aren't in this class"
        )

    recorded = 0
    skipped = 0

    for record in bulk_data.records:
        query_filters = [Attendance.school_id == school_id] if school_id else []
        existing = await session.execute(
            select(Attendance).where(
                *query_filters,
                Attendance.student_id == record.get("student_id"),
                Attendance.attendance_date == bulk_data.attendance_date
            )
        )
        existing_row = existing.scalar_one_or_none()

        if existing_row and not _is_auto_sourced(existing_row.recorded_by):
            skipped += 1
            continue

        if existing_row:
            # Auto-sourced placeholder — the teacher's bulk mark overwrites
            # it rather than being silently dropped like a real duplicate.
            existing_row.class_id = bulk_data.class_id
            existing_row.academic_term_id = bulk_data.academic_term_id
            existing_row.status = record.get("status", AttendanceStatus.PRESENT)
            existing_row.remarks = record.get("remarks")
            existing_row.recorded_by = current_user.id
            session.add(existing_row)
        else:
            attendance = Attendance(
                school_id=school_id,
                class_id=bulk_data.class_id,
                academic_term_id=bulk_data.academic_term_id,
                student_id=record.get("student_id"),
                attendance_date=bulk_data.attendance_date,
                status=record.get("status", AttendanceStatus.PRESENT),
                remarks=record.get("remarks"),
                recorded_by=current_user.id
            )
            session.add(attendance)
        recorded += 1

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Someone else just recorded attendance for one of these students on this date — please retry"
        )

    return {
        "message": f"Attendance recorded for {recorded} students",
        "recorded": recorded,
        "skipped": skipped
    }


@router.get("/class/{class_id}", response_model=dict)
async def get_class_attendance(
    class_id: str,
    attendance_date: str,
    current_user: User = Depends(require_permission("attendance.class_roster.view")),
    session: AsyncSession = Depends(get_session)
):
    """Get attendance for a class on a specific date — includes each student's parent
    contact info, so this is restricted to staff roles, not just anyone logged in."""
    school_id = current_user.school_id
    if not school_id and current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="No school context")

    class_result = await session.execute(select(Class).where(Class.id == class_id))
    cls = class_result.scalar_one_or_none()
    if not cls:
        raise HTTPException(status_code=404, detail="Class not found")

    if current_user.role != UserRole.SUPER_ADMIN and cls.school_id != school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    assert_campus_access(current_user, cls.campus_id)

    # active only — a student's class_id is never cleared when they exit,
    # so a roll-call roster without this filter would still list students
    # who graduated/transferred/withdrew, same bug already fixed for
    # routers/classes.py's roster endpoints.
    students_result = await session.execute(
        select(Student).where(Student.class_id == class_id, Student.status == StudentStatus.ACTIVE).order_by(Student.first_name)
    )
    students = students_result.scalars().all()

    attendance_result = await session.execute(
        select(Attendance).where(
            Attendance.class_id == class_id,
            Attendance.attendance_date == attendance_date
        )
    )
    attendance_records = {a.student_id: a for a in attendance_result.scalars().all()}
    
    # Get parent information for all students
    student_ids = [s.id for s in students]
    student_parents_result = await session.execute(
        select(StudentParent, Parent).where(
            StudentParent.student_id.in_(student_ids),
            StudentParent.parent_id == Parent.id
        )
    )
    student_parents_data = student_parents_result.all()
    
    # Map student to parent info (using first parent as primary)
    student_to_parent = {}
    for sp, parent in student_parents_data:
        if sp.student_id not in student_to_parent:
            student_to_parent[sp.student_id] = {
                "parent_name": f"{parent.first_name} {parent.last_name}",
                "parent_phone": parent.phone
            }
    
    records = []
    present = 0
    absent = 0
    late = 0
    excused = 0
    
    for student in students:
        att = attendance_records.get(student.id)
        status = att.status if att else None
        
        if status == AttendanceStatus.PRESENT:
            present += 1
        elif status == AttendanceStatus.ABSENT:
            absent += 1
        elif status == AttendanceStatus.LATE:
            late += 1
        elif status == AttendanceStatus.EXCUSED:
            excused += 1
        
        # Get parent info
        parent_info = student_to_parent.get(student.id, {})
        
        records.append({
            "student_id": student.id,
            "attendance_id": att.id if att else None,
            "student_name": f"{student.first_name} {student.last_name}",
            "photo_url": student.photo_url,
            "status": status,
            "remarks": att.remarks if att else None,
            "recorded": att is not None,
            "parent_name": parent_info.get("parent_name", "Unknown"),
            "parent_phone": parent_info.get("parent_phone", "")
        })
    
    return {
        "class_id": class_id,
        "class_name": cls.name,
        "date": attendance_date,
        "total_students": len(students),
        "summary": {
            "present": present,
            "absent": absent,
            "late": late,
            "excused": excused,
            "not_recorded": len(students) - (present + absent + late + excused)
        },
        "records": records
    }


@router.get("/student/{student_id}", response_model=dict)
async def get_student_attendance(
    student_id: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get attendance history for a student"""
    student_result = await session.execute(select(Student).where(Student.id == student_id))
    student = student_result.scalar_one_or_none()

    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    if current_user.role in (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.TEACHER):
        if current_user.role != UserRole.SUPER_ADMIN and current_user.school_id != student.school_id:
            raise HTTPException(status_code=403, detail="Access denied")
        assert_campus_access(current_user, student.campus_id)
    elif current_user.role == UserRole.STUDENT:
        if current_user.id != student.user_id:
            raise HTTPException(status_code=403, detail="Access denied")
    elif current_user.role == UserRole.PARENT:
        parent_result = await session.execute(select(Parent).where(Parent.user_id == current_user.id))
        parent = parent_result.scalar_one_or_none()
        has_access = False
        if parent:
            link_result = await session.execute(
                select(StudentParent).where(
                    StudentParent.parent_id == parent.id,
                    StudentParent.student_id == student_id
                )
            )
            has_access = link_result.scalar_one_or_none() is not None
        if not has_access:
            raise HTTPException(status_code=403, detail="Access denied")
    else:
        raise HTTPException(status_code=403, detail="Access denied")

    query = select(Attendance).where(Attendance.student_id == student_id)
    
    if start_date:
        query = query.where(Attendance.attendance_date >= start_date)
    if end_date:
        query = query.where(Attendance.attendance_date <= end_date)
    
    query = query.order_by(Attendance.attendance_date.desc())
    
    result = await session.execute(query)
    records = result.scalars().all()
    
    total = len(records)
    present = sum(1 for r in records if r.status == AttendanceStatus.PRESENT)
    absent = sum(1 for r in records if r.status == AttendanceStatus.ABSENT)
    late_count = sum(1 for r in records if r.status == AttendanceStatus.LATE)
    excused_count = sum(1 for r in records if r.status == AttendanceStatus.EXCUSED)
    
    return {
        "student_id": student_id,
        "student_name": f"{student.first_name} {student.last_name}",
        "summary": {
            "total_days": total,
            "present": present,
            "absent": absent,
            "late": late_count,
            "excused": excused_count,
            "attendance_percentage": round((present + late_count) / total * 100, 1) if total > 0 else 0
        },
        "records": [
            {
                "id": r.id,
                "date": r.attendance_date,
                "status": r.status,
                "remarks": r.remarks
            }
            for r in records
        ]
    }


@router.put("/{attendance_id}", response_model=dict)
async def update_attendance(
    attendance_id: str,
    body: UpdateAttendanceRequest,
    current_user: User = Depends(require_permission("attendance.mark.update")),
    session: AsyncSession = Depends(get_session)
):
    status = body.status
    remarks = body.remarks
    """Update an attendance record"""
    result = await session.execute(select(Attendance).where(Attendance.id == attendance_id))
    attendance = result.scalar_one_or_none()
    
    if not attendance:
        raise HTTPException(status_code=404, detail="Attendance record not found")
    
    if current_user.role not in [UserRole.SUPER_ADMIN] and current_user.school_id != attendance.school_id:
        raise HTTPException(status_code=403, detail="Access denied")

    class_result = await session.execute(select(Class).where(Class.id == attendance.class_id))
    cls = class_result.scalar_one_or_none()
    if cls:
        assert_campus_access(current_user, cls.campus_id)

    # record_attendance/record_bulk_attendance both require a TEACHER to be
    # actually assigned to the class via validate_attendance_context; this
    # correction endpoint skipped that check entirely, so a TEACHER holding
    # the generic attendance.mark.update permission could alter any
    # student's record anywhere in the school, not just for classes they
    # teach.
    if current_user.role == UserRole.TEACHER:
        staff_result = await session.execute(select(Staff).where(Staff.user_id == current_user.id))
        staff = staff_result.scalar_one_or_none()
        assignment = None
        if staff:
            assignment_result = await session.execute(
                select(TeacherAssignment).where(
                    TeacherAssignment.staff_id == staff.id,
                    TeacherAssignment.class_id == attendance.class_id
                )
            )
            assignment = assignment_result.scalars().first()
        if not assignment:
            raise HTTPException(status_code=403, detail="You are not assigned to teach this class")

    old_status = attendance.status
    attendance.status = status
    attendance.remarks = remarks
    attendance.updated_at = datetime.utcnow()
    session.add(attendance)
    await session.commit()

    await log_event(
        session, actor=current_user, action="attendance.corrected", entity_type="attendance",
        entity_id=attendance_id, school_id=attendance.school_id,
        summary=f"{current_user.email} changed an attendance record for student {attendance.student_id} from {old_status} to {status}",
        old_values={"status": old_status}, new_values={"status": status},
    )

    return {"message": "Attendance updated"}


# ── Staff attendance ────────────────────────────────────────────────────────
# Before this, StaffAttendance was a table nobody ever wrote to — payroll's
# attendance-based deduction rules always evaluated against zero real data.
# This gives HR/admin a way to actually mark it, and feeds the exact same
# table PayrollService.get_staff_attendance_counts() already reads from.

def _summarize_staff_attendance(records: list) -> dict:
    total = len(records)
    present = sum(1 for r in records if r.status == AttendanceStatus.PRESENT)
    absent = sum(1 for r in records if r.status == AttendanceStatus.ABSENT)
    late = sum(1 for r in records if r.status == AttendanceStatus.LATE)
    excused = sum(1 for r in records if r.status == AttendanceStatus.EXCUSED)
    return {
        "total_days": total,
        "present": present,
        "absent": absent,
        "late": late,
        "excused": excused,
        "attendance_percentage": round((present + late) / total * 100, 1) if total > 0 else 0,
    }


@router.get("/staff-roster", response_model=dict)
async def get_staff_attendance_roster(
    attendance_date: str,
    current_user: User = Depends(require_permission("staff_attendance.roster.view")),
    _plan_check: User = Depends(require_plan_feature("staff_attendance")),
    session: AsyncSession = Depends(get_session)
):
    """Every active staff member and their attendance status for one day —
    the roster the marking UI is built around, mirroring get_class_attendance
    for students."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    staff_query = select(Staff).where(Staff.school_id == school_id, Staff.status == StaffStatus.ACTIVE)
    campus_id = resolve_campus_scope(current_user)
    if campus_id:
        staff_query = staff_query.where(Staff.campus_id == campus_id)
    staff_result = await session.execute(staff_query.order_by(Staff.first_name))
    staff_list = staff_result.scalars().all()

    attendance_result = await session.execute(
        select(StaffAttendance).where(
            StaffAttendance.school_id == school_id,
            StaffAttendance.attendance_date == attendance_date,
        )
    )
    records = {a.staff_id: a for a in attendance_result.scalars().all()}

    rows = []
    for staff in staff_list:
        att = records.get(staff.id)
        rows.append({
            "staff_id": staff.id,
            "staff_name": f"{staff.first_name} {staff.last_name}",
            "position": staff.position,
            "shift_id": staff.shift_id,
            "attendance_id": att.id if att else None,
            "status": att.status if att else None,
            "check_in": att.check_in if att else None,
            "check_out": att.check_out if att else None,
            "remarks": att.remarks if att else None,
            "recorded": att is not None,
            "ip_flagged": att.ip_flagged if att else False,
            "via_qr": att.via_qr if att else False,
            "self_recorded": bool(att and att.recorded_by == staff.user_id and staff.user_id) if att else False,
        })

    return {
        "date": attendance_date,
        "total_staff": len(staff_list),
        "recorded": sum(1 for r in rows if r["recorded"]),
        "records": rows,
    }


@router.post("/staff", response_model=dict)
async def record_staff_attendance(
    attendance_data: StaffAttendanceCreate,
    current_user: User = Depends(require_permission("staff_attendance.roster.create")),
    _plan_check: User = Depends(require_plan_feature("staff_attendance")),
    session: AsyncSession = Depends(get_session)
):
    """Mark attendance for one staff member on one day."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    staff_result = await session.execute(select(Staff).where(Staff.id == attendance_data.staff_id, Staff.school_id == school_id))
    staff = staff_result.scalar_one_or_none()
    if not staff:
        raise HTTPException(status_code=400, detail="staff_id does not exist for this school")
    assert_campus_access(current_user, staff.campus_id)

    existing = await session.execute(
        select(StaffAttendance).where(
            StaffAttendance.school_id == school_id,
            StaffAttendance.staff_id == attendance_data.staff_id,
            StaffAttendance.attendance_date == attendance_data.attendance_date,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Attendance already recorded for this staff member on this date")

    attendance = StaffAttendance(
        school_id=school_id,
        recorded_by=current_user.id,
        **attendance_data.model_dump()
    )
    session.add(attendance)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=400, detail="Attendance already recorded for this staff member on this date")
    await session.refresh(attendance)

    return {
        "id": attendance.id,
        "staff_id": attendance.staff_id,
        "date": attendance.attendance_date,
        "status": attendance.status,
        "message": "Staff attendance recorded"
    }


@router.post("/staff/bulk", response_model=dict)
async def record_bulk_staff_attendance(
    bulk_data: StaffAttendanceBulkCreate,
    current_user: User = Depends(require_permission("staff_attendance.roster.create")),
    _plan_check: User = Depends(require_plan_feature("staff_attendance")),
    session: AsyncSession = Depends(get_session)
):
    """Mark attendance for the whole staff roster (or a subset) on one day."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    recorded = 0
    skipped = 0

    for record in bulk_data.records:
        staff_id = record.get("staff_id")
        if not staff_id:
            skipped += 1
            continue

        staff_result = await session.execute(select(Staff).where(Staff.id == staff_id, Staff.school_id == school_id))
        staff = staff_result.scalar_one_or_none()
        if not staff:
            skipped += 1
            continue
        try:
            assert_campus_access(current_user, staff.campus_id)
        except HTTPException:
            skipped += 1
            continue

        existing = await session.execute(
            select(StaffAttendance).where(
                StaffAttendance.school_id == school_id,
                StaffAttendance.staff_id == staff_id,
                StaffAttendance.attendance_date == bulk_data.attendance_date,
            )
        )
        if existing.scalar_one_or_none():
            skipped += 1
            continue

        session.add(StaffAttendance(
            school_id=school_id,
            staff_id=staff_id,
            attendance_date=bulk_data.attendance_date,
            status=record.get("status", AttendanceStatus.PRESENT),
            check_in=record.get("check_in"),
            check_out=record.get("check_out"),
            remarks=record.get("remarks"),
            recorded_by=current_user.id,
        ))
        # Committed per-row (not batched into one commit at the end) so a
        # uq_staff_attendance_staff_date race on one row -- a concurrent
        # request for the same staff/day slipping in between the check
        # above and this write -- only rolls back that one row instead of
        # the whole batch, consistent with this endpoint's existing
        # skip-and-count semantics for every other kind of invalid row.
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            skipped += 1
            continue
        recorded += 1

    return {
        "message": f"Attendance recorded for {recorded} staff member(s)",
        "recorded": recorded,
        "skipped": skipped,
    }


@router.get("/staff/my", response_model=dict)
async def get_my_staff_attendance(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("staff_attendance")),
    session: AsyncSession = Depends(get_session)
):
    """Self-service — a staff member's own attendance history."""
    staff_result = await session.execute(select(Staff).where(Staff.user_id == current_user.id))
    staff = staff_result.scalar_one_or_none()
    if not staff:
        raise HTTPException(status_code=404, detail="No staff profile linked to this account")

    query = select(StaffAttendance).where(StaffAttendance.staff_id == staff.id)
    if start_date:
        query = query.where(StaffAttendance.attendance_date >= start_date)
    if end_date:
        query = query.where(StaffAttendance.attendance_date <= end_date)
    query = query.order_by(StaffAttendance.attendance_date.desc())

    result = await session.execute(query)
    records = result.scalars().all()

    return {
        "staff_id": staff.id,
        "staff_name": f"{staff.first_name} {staff.last_name}",
        "summary": _summarize_staff_attendance(records),
        "records": [
            {"id": r.id, "date": r.attendance_date, "status": r.status, "check_in": r.check_in, "check_out": r.check_out, "remarks": r.remarks}
            for r in records
        ],
    }


@router.get("/staff/export", response_class=StreamingResponse)
async def export_staff_attendance(
    start_date: str,
    end_date: str,
    current_user: User = Depends(require_permission("staff_attendance.roster.view")),
    _plan_check: User = Depends(require_plan_feature("staff_attendance")),
    session: AsyncSession = Depends(get_session)
):
    if not current_user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    csv_content = await generate_staff_attendance_csv(session, current_user.school_id, start_date, end_date)
    filename = f"staff_attendance_{start_date}_to_{end_date}.csv"
    return StreamingResponse(
        iter([csv_content]), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


# ── Shifts & late-cutoff ─────────────────────────────────────────────────────
# Resolves PRESENT vs LATE on self clock-in by comparing against the staff
# member's assigned shift (falling back to the school's default shift, then
# to always-PRESENT if neither is configured — the exact behavior before
# shifts existed, so schools that never touch this feature see no change).

def _serialize_shift(shift: Shift) -> dict:
    return {
        "id": shift.id,
        "name": shift.name,
        "start_time": shift.start_time,
        "end_time": shift.end_time,
        "late_grace_minutes": shift.late_grace_minutes,
        "is_default": shift.is_default,
        "is_active": shift.is_active,
    }


async def _get_shift_or_404(session: AsyncSession, school_id: str, shift_id: str) -> Shift:
    result = await session.execute(select(Shift).where(Shift.id == shift_id, Shift.school_id == school_id))
    shift = result.scalar_one_or_none()
    if not shift:
        raise HTTPException(status_code=404, detail="Shift not found")
    return shift


async def _resolve_staff_in_school(session: AsyncSession, school_id: str, staff_id: str) -> Staff:
    result = await session.execute(select(Staff).where(Staff.id == staff_id, Staff.school_id == school_id))
    staff = result.scalar_one_or_none()
    if not staff:
        raise HTTPException(status_code=404, detail="Staff member not found")
    return staff


async def _resolve_clock_in_status(session: AsyncSession, staff: Staff, clock_in_time_str: str) -> AttendanceStatus:
    shift = None
    if staff.shift_id:
        result = await session.execute(
            select(Shift).where(Shift.id == staff.shift_id, Shift.school_id == staff.school_id, Shift.is_active == True)
        )
        shift = result.scalar_one_or_none()
    if not shift:
        result = await session.execute(
            select(Shift).where(Shift.school_id == staff.school_id, Shift.is_default == True, Shift.is_active == True)
        )
        shift = result.scalar_one_or_none()
    if not shift:
        return AttendanceStatus.PRESENT

    clock_in_time = datetime.strptime(clock_in_time_str, "%H:%M:%S")
    shift_start = datetime.strptime(shift.start_time, "%H:%M:%S")
    cutoff = shift_start + timedelta(minutes=shift.late_grace_minutes)
    return AttendanceStatus.LATE if clock_in_time > cutoff else AttendanceStatus.PRESENT


async def record_staff_punch(
    session: AsyncSession,
    background_tasks: BackgroundTasks,
    school_id: str,
    staff: Staff,
    device_serial: str,
    punched_at: Optional[datetime] = None,
    event_type: Optional[str] = None,
) -> dict:
    """A fingerprint/face-scan device reporting a staff punch — shared by
    routers/public_api.py's API-key-authenticated endpoint (vendor
    middleware that already knows our staff_id) and
    routers/biometric_adms.py's raw ADMS bridge (a device with no
    middleware in between, resolved via Staff.biometric_device_pin).
    Auto-toggles check-in vs check-out when event_type is omitted (matches
    how most punch-clock devices work — they report a raw scan, not a
    declared direction): no record yet today, or one with no check_in, is
    a check-in; a record with check_in but no check_out is a check-out; a
    third scan the same day is a no-op. An explicit event_type overrides
    the toggle for devices that do distinguish an IN/OUT button press."""
    punched_at = punched_at or datetime.utcnow()
    attendance_date = punched_at.strftime("%Y-%m-%d")
    time_str = punched_at.strftime("%H:%M:%S")

    existing_result = await session.execute(
        select(StaffAttendance).where(StaffAttendance.staff_id == staff.id, StaffAttendance.attendance_date == attendance_date)
    )
    record = existing_result.scalar_one_or_none()

    direction = event_type
    if direction not in ("check_in", "check_out"):
        if not record or not record.check_in:
            direction = "check_in"
        elif not record.check_out:
            direction = "check_out"
        else:
            direction = None  # already fully punched for the day — no-op

    recorded_by = f"biometric_device:{device_serial}"

    if direction == "check_in":
        clock_in_status = await _resolve_clock_in_status(session, staff, time_str)
        if record:
            record.check_in = time_str
            record.status = clock_in_status
            record.recorded_by = recorded_by
            record.updated_at = datetime.utcnow()
            session.add(record)
        else:
            record = StaffAttendance(
                school_id=school_id, staff_id=staff.id, attendance_date=attendance_date,
                check_in=time_str, status=clock_in_status, recorded_by=recorded_by,
            )
            session.add(record)
    elif direction == "check_out":
        record.check_out = time_str
        record.updated_at = datetime.utcnow()
        session.add(record)

    try:
        await session.commit()
    except IntegrityError:
        # Two near-simultaneous punches for the same staff/day both saw "no
        # existing row" and both tried to insert — uq_staff_attendance_staff_date
        # catches the loser. Treat it the same as the "already fully punched"
        # no-op case above rather than raising, since this function is a
        # background service call (from public_api.py / biometric_adms.py),
        # not a router endpoint with a human waiting on an error response.
        await session.rollback()
        direction = None
        existing_result = await session.execute(
            select(StaffAttendance).where(StaffAttendance.staff_id == staff.id, StaffAttendance.attendance_date == attendance_date)
        )
        record = existing_result.scalar_one_or_none()
    if record:
        await session.refresh(record)

    if direction:
        await emit_event(
            session, background_tasks, school_id, "staff_attendance.marked",
            {
                "id": record.id, "staff_id": record.staff_id, "date": record.attendance_date,
                "check_in": record.check_in, "check_out": record.check_out, "status": record.status,
            },
        )

    return {
        "id": record.id if record else None,
        "staff_id": staff.id,
        "date": attendance_date,
        "direction": direction,
        "check_in": record.check_in if record else None,
        "check_out": record.check_out if record else None,
        "already_recorded": direction is None,
    }


class StaffBiometricPinRequest(SQLModel):
    pin: str


@router.put("/staff/{staff_id}/biometric-pin", response_model=dict)
async def set_staff_biometric_pin(
    staff_id: str,
    body: StaffBiometricPinRequest,
    current_user: User = Depends(require_permission("staff_attendance.pin.manage")),
    _plan_check: User = Depends(require_plan_feature("staff_attendance")),
    session: AsyncSession = Depends(get_session),
):
    """Records the PIN HR assigned this staff member at a fingerprint/face
    terminal's own (local) enrollment step — this endpoint never talks to
    the device itself, it just stores the mapping our side needs (see
    routers/biometric_adms.py)."""
    staff = await _resolve_staff_in_school(session, current_user.school_id, staff_id)
    staff.biometric_device_pin = body.pin
    staff.updated_at = datetime.utcnow()
    session.add(staff)
    await session.commit()
    return {"ok": True}


@router.post("/shifts", response_model=dict)
async def create_shift(
    data: ShiftCreate,
    current_user: User = Depends(require_permission("staff_attendance.shift.manage")),
    _plan_check: User = Depends(require_plan_feature("staff_attendance")),
    session: AsyncSession = Depends(get_session)
):
    if not current_user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    shift = Shift(school_id=current_user.school_id, **data.model_dump())
    session.add(shift)
    await session.commit()
    await session.refresh(shift)
    return _serialize_shift(shift)


@router.get("/shifts", response_model=dict)
async def list_shifts(
    include_inactive: bool = False,
    current_user: User = Depends(require_permission("staff_attendance.shift.view")),
    _plan_check: User = Depends(require_plan_feature("staff_attendance")),
    session: AsyncSession = Depends(get_session)
):
    if not current_user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    query = select(Shift).where(Shift.school_id == current_user.school_id)
    if not include_inactive:
        query = query.where(Shift.is_active == True)
    result = await session.execute(query.order_by(Shift.name))
    return {"records": [_serialize_shift(s) for s in result.scalars().all()]}


@router.get("/shifts/{shift_id}", response_model=dict)
async def get_shift(
    shift_id: str,
    current_user: User = Depends(require_permission("staff_attendance.shift.view")),
    _plan_check: User = Depends(require_plan_feature("staff_attendance")),
    session: AsyncSession = Depends(get_session)
):
    shift = await _get_shift_or_404(session, current_user.school_id, shift_id)
    return _serialize_shift(shift)


@router.put("/shifts/{shift_id}", response_model=dict)
async def update_shift(
    shift_id: str,
    body: ShiftUpdate,
    current_user: User = Depends(require_permission("staff_attendance.shift.manage")),
    _plan_check: User = Depends(require_plan_feature("staff_attendance")),
    session: AsyncSession = Depends(get_session)
):
    shift = await _get_shift_or_404(session, current_user.school_id, shift_id)
    update_data = body.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(shift, key, value)
    shift.updated_at = datetime.utcnow()
    session.add(shift)
    await session.commit()
    return _serialize_shift(shift)


@router.delete("/shifts/{shift_id}", response_model=dict)
async def delete_shift(
    shift_id: str,
    current_user: User = Depends(require_permission("staff_attendance.shift.manage")),
    _plan_check: User = Depends(require_plan_feature("staff_attendance")),
    session: AsyncSession = Depends(get_session)
):
    """Soft delete — staff assigned to it keep their shift_id, which simply
    stops resolving to an active shift (falls back to the school default /
    always-PRESENT, same as an unassigned staff member)."""
    shift = await _get_shift_or_404(session, current_user.school_id, shift_id)
    shift.is_active = False
    shift.is_default = False
    shift.updated_at = datetime.utcnow()
    session.add(shift)
    await session.commit()
    return {"message": "Shift deactivated"}


@router.post("/shifts/{shift_id}/set-default", response_model=dict)
async def set_default_shift(
    shift_id: str,
    current_user: User = Depends(require_permission("staff_attendance.shift.manage")),
    _plan_check: User = Depends(require_plan_feature("staff_attendance")),
    session: AsyncSession = Depends(get_session)
):
    shift = await _get_shift_or_404(session, current_user.school_id, shift_id)
    others_result = await session.execute(
        select(Shift).where(Shift.school_id == current_user.school_id, Shift.id != shift_id, Shift.is_default == True)
    )
    for other in others_result.scalars().all():
        other.is_default = False
        session.add(other)
    shift.is_default = True
    shift.updated_at = datetime.utcnow()
    session.add(shift)
    await session.commit()
    return _serialize_shift(shift)


@router.put("/staff/{staff_id}/shift", response_model=dict)
async def assign_staff_shift(
    staff_id: str,
    body: ShiftAssign,
    current_user: User = Depends(require_permission("staff_attendance.shift.assign")),
    _plan_check: User = Depends(require_plan_feature("staff_attendance")),
    session: AsyncSession = Depends(get_session)
):
    staff = await _resolve_staff_in_school(session, current_user.school_id, staff_id)
    if body.shift_id:
        await _get_shift_or_404(session, current_user.school_id, body.shift_id)
    old_shift_id = staff.shift_id
    staff.shift_id = body.shift_id
    staff.updated_at = datetime.utcnow()
    session.add(staff)
    if old_shift_id != body.shift_id:
        session.add(StaffShiftHistory(school_id=current_user.school_id, staff_id=staff.id, old_shift_id=old_shift_id, new_shift_id=body.shift_id, changed_by=current_user.id))
    await session.commit()
    return {"message": "Shift assignment updated", "staff_id": staff.id, "shift_id": staff.shift_id}


@router.post("/staff/shift/bulk-assign", response_model=dict)
async def bulk_assign_staff_shift(
    body: ShiftBulkAssign,
    current_user: User = Depends(require_permission("staff_attendance.shift.assign")),
    _plan_check: User = Depends(require_plan_feature("staff_attendance")),
    session: AsyncSession = Depends(get_session)
):
    if body.shift_id:
        await _get_shift_or_404(session, current_user.school_id, body.shift_id)
    result = await session.execute(
        select(Staff).where(Staff.school_id == current_user.school_id, Staff.id.in_(body.staff_ids))
    )
    staff_list = result.scalars().all()
    for staff in staff_list:
        old_shift_id = staff.shift_id
        staff.shift_id = body.shift_id
        staff.updated_at = datetime.utcnow()
        session.add(staff)
        if old_shift_id != body.shift_id:
            session.add(StaffShiftHistory(school_id=current_user.school_id, staff_id=staff.id, old_shift_id=old_shift_id, new_shift_id=body.shift_id, changed_by=current_user.id))
    await session.commit()
    return {"message": f"Shift assignment updated for {len(staff_list)} staff member(s)", "updated": len(staff_list)}


@router.get("/staff/{staff_id}/shift-history", response_model=list[dict])
async def get_staff_shift_history(
    staff_id: str,
    current_user: User = Depends(require_permission("staff_attendance.shift.assign")),
    session: AsyncSession = Depends(get_session),
):
    """Previously no history existed at all — models/shift.py's own
    docstring said "reassigning takes effect immediately" with nothing
    kept about who changed it or when."""
    await _resolve_staff_in_school(session, current_user.school_id, staff_id)
    result = await session.execute(
        select(StaffShiftHistory).where(StaffShiftHistory.school_id == current_user.school_id, StaffShiftHistory.staff_id == staff_id)
        .order_by(StaffShiftHistory.changed_at.desc())
    )
    return [item.model_dump() for item in result.scalars().all()]


@router.post("/staff/auto-absent", response_model=dict)
async def mark_auto_absent(
    body: AutoAbsentRequest,
    current_user: User = Depends(require_permission("staff_attendance.roster.update")),
    _plan_check: User = Depends(require_plan_feature("staff_attendance")),
    session: AsyncSession = Depends(get_session)
):
    """Marks ABSENT every active staff member with no attendance row for a
    given date (defaults to yesterday). This codebase has no scheduler
    infrastructure (see services/subscription_suspension_service.py) — like
    that endpoint, this is meant to be invoked periodically by an external
    cron caller, not a background job running inside this process. Weekends
    and school holidays (CalendarEvent with is_instructional=False) are both
    skipped."""
    if not current_user.school_id:
        raise HTTPException(status_code=403, detail="No school context")

    target_date = body.attendance_date or (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")

    holiday_dates = await fetch_holiday_dates(session, current_user.school_id)
    if not is_working_day(target_date, holiday_dates):
        reason = "a school holiday" if target_date in holiday_dates else "a weekend"
        return {
            "date": target_date,
            "message": f"{target_date} is {reason}, nothing to do",
            "marked_absent": 0,
            "already_recorded": 0,
            "total_active_staff": 0,
        }

    staff_result = await session.execute(
        select(Staff).where(Staff.school_id == current_user.school_id, Staff.status == StaffStatus.ACTIVE)
    )
    staff_list = staff_result.scalars().all()

    existing_result = await session.execute(
        select(StaffAttendance.staff_id).where(
            StaffAttendance.school_id == current_user.school_id,
            StaffAttendance.attendance_date == target_date,
        )
    )
    already_recorded_ids = set(existing_result.scalars().all())

    marked = 0
    for staff in staff_list:
        if staff.id in already_recorded_ids:
            continue
        session.add(StaffAttendance(
            school_id=current_user.school_id,
            staff_id=staff.id,
            attendance_date=target_date,
            status=AttendanceStatus.ABSENT,
            recorded_by="SYSTEM",
            remarks="Auto-marked absent — no attendance recorded",
        ))
        marked += 1

    await session.commit()

    return {
        "date": target_date,
        "marked_absent": marked,
        "already_recorded": len(already_recorded_ids),
        "total_active_staff": len(staff_list),
    }


# ── Self-service clock-in/out ───────────────────────────────────────────────
# Everything above this point is HR/admin retroactively typing in attendance
# after the fact. This is the actual "attendance system" part — a staff
# member taps a button, the server timestamps it itself (never trusts a
# client-supplied time), and each school chooses how strict to be about
# proving the tap happened on-site: nothing (just log the IP for later
# review), or require scanning a QR code posted at the gate.

def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _is_ip_flagged(ip: str, allowed_cidr: Optional[str]) -> bool:
    """True only when the school has configured an allowlist AND this IP
    falls outside every range in it. No allowlist configured = never flagged
    (mode is purely informational until a school opts in)."""
    if not allowed_cidr or ip == "unknown":
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # unparseable IP is itself worth flagging
    for cidr in allowed_cidr.split(","):
        cidr = cidr.strip()
        if not cidr:
            continue
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return False
        except ValueError:
            continue
    return True


async def _get_or_create_settings(session: AsyncSession, school_id: str) -> StaffAttendanceSettings:
    result = await session.execute(select(StaffAttendanceSettings).where(StaffAttendanceSettings.school_id == school_id))
    settings = result.scalar_one_or_none()
    if not settings:
        settings = StaffAttendanceSettings(school_id=school_id)
        session.add(settings)
        await session.flush()
    return settings


def _serialize_settings(settings: StaffAttendanceSettings) -> dict:
    # Returns the raw token, not a full URL — the frontend already knows its
    # own origin (window.location.origin), which is correct whether it's
    # running on localhost, a staging domain, or production. Baking a
    # public URL into the backend would just be one more thing to
    # misconfigure per environment.
    token_valid = False
    if settings.current_qr_token and settings.qr_token_generated_at:
        expires_at = settings.qr_token_generated_at + timedelta(minutes=settings.qr_token_ttl_minutes)
        token_valid = datetime.utcnow() < expires_at
    return {
        "clock_in_mode": settings.clock_in_mode,
        "allowed_ip_cidr": settings.allowed_ip_cidr,
        "qr_token_ttl_minutes": settings.qr_token_ttl_minutes,
        "qr_token_valid": token_valid,
        "current_qr_token": settings.current_qr_token if token_valid else None,
        "qr_token_generated_at": settings.qr_token_generated_at.isoformat() if settings.qr_token_generated_at else None,
    }


@router.get("/staff/settings", response_model=dict)
async def get_staff_attendance_settings(
    current_user: User = Depends(require_permission("staff_attendance.settings.manage")),
    _plan_check: User = Depends(require_plan_feature("staff_attendance")),
    session: AsyncSession = Depends(get_session)
):
    if not current_user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    settings = await _get_or_create_settings(session, current_user.school_id)
    await session.commit()
    return _serialize_settings(settings)


@router.put("/staff/settings", response_model=dict)
async def update_staff_attendance_settings(
    body: StaffAttendanceSettingsUpdate,
    current_user: User = Depends(require_permission("staff_attendance.settings.manage")),
    _plan_check: User = Depends(require_plan_feature("staff_attendance")),
    session: AsyncSession = Depends(get_session)
):
    if not current_user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    settings = await _get_or_create_settings(session, current_user.school_id)

    update_data = body.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(settings, key, value)
    settings.updated_at = datetime.utcnow()
    settings.updated_by = current_user.id
    session.add(settings)
    await session.commit()

    return _serialize_settings(settings)


@router.post("/staff/settings/qr-token/rotate", response_model=dict)
async def rotate_staff_qr_token(
    current_user: User = Depends(require_permission("staff_attendance.settings.manage")),
    _plan_check: User = Depends(require_plan_feature("staff_attendance")),
    session: AsyncSession = Depends(get_session)
):
    """Issue a new gate QR token, invalidating whatever was printed before —
    the fix if a poster's code leaks or just gets rotated on a schedule."""
    if not current_user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    settings = await _get_or_create_settings(session, current_user.school_id)

    settings.current_qr_token = secrets.token_urlsafe(24)
    settings.qr_token_generated_at = datetime.utcnow()
    settings.updated_at = datetime.utcnow()
    settings.updated_by = current_user.id
    session.add(settings)
    await session.commit()

    return _serialize_settings(settings)


async def _resolve_current_staff(session: AsyncSession, current_user: User) -> Staff:
    staff_result = await session.execute(select(Staff).where(Staff.user_id == current_user.id))
    staff = staff_result.scalar_one_or_none()
    if not staff:
        raise HTTPException(status_code=404, detail="No staff profile linked to this account")
    return staff


def _validate_qr_token(settings: StaffAttendanceSettings, provided_token: Optional[str]) -> None:
    if settings.clock_in_mode != ClockInMode.QR_REQUIRED:
        return
    if not provided_token:
        raise HTTPException(status_code=400, detail="Scan the QR code at the school entrance to clock in/out")
    if not settings.current_qr_token or provided_token != settings.current_qr_token:
        raise HTTPException(status_code=400, detail="This QR code is invalid — scan the current code at the gate")
    if not settings.qr_token_generated_at:
        raise HTTPException(status_code=400, detail="This QR code has expired — ask HR to print the latest one")
    expires_at = settings.qr_token_generated_at + timedelta(minutes=settings.qr_token_ttl_minutes)
    if datetime.utcnow() >= expires_at:
        raise HTTPException(status_code=400, detail="This QR code has expired — ask HR to print the latest one")


@router.get("/staff/today", response_model=dict)
async def get_today_staff_attendance(
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("staff_attendance")),
    session: AsyncSession = Depends(get_session)
):
    """What the Clock In/Out button should show right now for this staff member."""
    staff = await _resolve_current_staff(session, current_user)
    today = datetime.utcnow().strftime("%Y-%m-%d")

    result = await session.execute(
        select(StaffAttendance).where(StaffAttendance.staff_id == staff.id, StaffAttendance.attendance_date == today)
    )
    record = result.scalar_one_or_none()

    settings = await _get_or_create_settings(session, staff.school_id)
    await session.commit()

    return {
        "date": today,
        "clock_in_mode": settings.clock_in_mode,
        "clocked_in": bool(record and record.check_in),
        "clocked_out": bool(record and record.check_out),
        "check_in": record.check_in if record else None,
        "check_out": record.check_out if record else None,
    }


@router.post("/staff/clock-in", response_model=dict)
async def clock_in(
    body: ClockRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("staff_attendance")),
    session: AsyncSession = Depends(get_session)
):
    staff = await _resolve_current_staff(session, current_user)
    settings = await _get_or_create_settings(session, staff.school_id)
    _validate_qr_token(settings, body.qr_token)

    today = datetime.utcnow().strftime("%Y-%m-%d")
    existing_result = await session.execute(
        select(StaffAttendance).where(StaffAttendance.staff_id == staff.id, StaffAttendance.attendance_date == today)
    )
    existing = existing_result.scalar_one_or_none()
    # Only a record with check_in already set means truly "already clocked in" —
    # an HR-entered record for today with no check-in time yet (e.g. pre-marked
    # present) shouldn't block the staff member's own clock-in.
    if existing and existing.check_in:
        raise HTTPException(status_code=400, detail="You've already clocked in today")

    ip = _get_client_ip(request)
    now_str = datetime.utcnow().strftime("%H:%M:%S")
    ip_flagged = _is_ip_flagged(ip, settings.allowed_ip_cidr)
    clock_in_status = await _resolve_clock_in_status(session, staff, now_str)

    if existing:
        existing.check_in = now_str
        existing.status = clock_in_status
        existing.clock_in_ip = ip
        existing.ip_flagged = ip_flagged
        existing.via_qr = bool(body.qr_token)
        existing.updated_at = datetime.utcnow()
        session.add(existing)
    else:
        record = StaffAttendance(
            school_id=staff.school_id,
            staff_id=staff.id,
            attendance_date=today,
            check_in=now_str,
            status=clock_in_status,
            recorded_by=current_user.id,
            clock_in_ip=ip,
            ip_flagged=ip_flagged,
            via_qr=bool(body.qr_token),
        )
        session.add(record)

    try:
        await session.commit()
    except IntegrityError:
        # Two concurrent clock-ins both saw "no existing row" and both tried
        # to insert — uq_staff_attendance_staff_date catches the loser.
        await session.rollback()
        raise HTTPException(status_code=400, detail="You've already clocked in today")

    return {"message": "Clocked in", "check_in": now_str, "status": clock_in_status, "ip_flagged": ip_flagged}


@router.post("/staff/clock-out", response_model=dict)
async def clock_out(
    body: ClockRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("staff_attendance")),
    session: AsyncSession = Depends(get_session)
):
    staff = await _resolve_current_staff(session, current_user)
    settings = await _get_or_create_settings(session, staff.school_id)
    _validate_qr_token(settings, body.qr_token)

    today = datetime.utcnow().strftime("%Y-%m-%d")
    result = await session.execute(
        select(StaffAttendance).where(StaffAttendance.staff_id == staff.id, StaffAttendance.attendance_date == today)
    )
    record = result.scalar_one_or_none()
    if not record or not record.check_in:
        raise HTTPException(status_code=400, detail="You haven't clocked in today yet")
    if record.check_out:
        raise HTTPException(status_code=400, detail="You've already clocked out today")

    ip = _get_client_ip(request)
    now_str = datetime.utcnow().strftime("%H:%M:%S")
    record.check_out = now_str
    record.clock_out_ip = ip
    if _is_ip_flagged(ip, settings.allowed_ip_cidr):
        record.ip_flagged = True
    record.updated_at = datetime.utcnow()
    session.add(record)
    await session.commit()

    return {"message": "Clocked out", "check_out": now_str, "ip_flagged": record.ip_flagged}


@router.get("/staff/{staff_id}", response_model=dict)
async def get_staff_attendance(
    staff_id: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("staff_attendance")),
    session: AsyncSession = Depends(get_session)
):
    """Attendance history for one staff member — admin/HR can view anyone in
    their school; a staff member can view their own."""
    staff_result = await session.execute(select(Staff).where(Staff.id == staff_id))
    staff = staff_result.scalar_one_or_none()
    if not staff:
        raise HTTPException(status_code=404, detail="Staff member not found")

    is_admin = current_user.role in STAFF_ATTENDANCE_ADMIN_ROLES and current_user.school_id == staff.school_id
    is_self = staff.user_id == current_user.id
    if current_user.role != UserRole.SUPER_ADMIN and not is_admin and not is_self:
        raise HTTPException(status_code=403, detail="Access denied")
    if is_admin:
        assert_campus_access(current_user, staff.campus_id)

    query = select(StaffAttendance).where(StaffAttendance.staff_id == staff_id)
    if start_date:
        query = query.where(StaffAttendance.attendance_date >= start_date)
    if end_date:
        query = query.where(StaffAttendance.attendance_date <= end_date)
    query = query.order_by(StaffAttendance.attendance_date.desc())

    result = await session.execute(query)
    records = result.scalars().all()

    return {
        "staff_id": staff_id,
        "staff_name": f"{staff.first_name} {staff.last_name}",
        "summary": _summarize_staff_attendance(records),
        "records": [
            {"id": r.id, "date": r.attendance_date, "status": r.status, "check_in": r.check_in, "check_out": r.check_out, "remarks": r.remarks}
            for r in records
        ],
    }


@router.put("/staff-records/{attendance_id}", response_model=dict)
async def update_staff_attendance(
    attendance_id: str,
    body: UpdateAttendanceRequest,
    current_user: User = Depends(require_permission("staff_attendance.roster.update")),
    _plan_check: User = Depends(require_plan_feature("staff_attendance")),
    session: AsyncSession = Depends(get_session)
):
    """Correct a staff attendance record."""
    result = await session.execute(select(StaffAttendance).where(StaffAttendance.id == attendance_id))
    attendance = result.scalar_one_or_none()

    if not attendance:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    if current_user.role != UserRole.SUPER_ADMIN and current_user.school_id != attendance.school_id:
        raise HTTPException(status_code=403, detail="Access denied")

    staff_result = await session.execute(select(Staff).where(Staff.id == attendance.staff_id))
    staff = staff_result.scalar_one_or_none()
    if staff:
        assert_campus_access(current_user, staff.campus_id)

    old_status = attendance.status
    attendance.status = body.status
    attendance.remarks = body.remarks
    attendance.updated_at = datetime.utcnow()
    session.add(attendance)
    await session.commit()

    await log_event(
        session, actor=current_user, action="staff_attendance.corrected", entity_type="staff_attendance",
        entity_id=attendance_id, school_id=attendance.school_id,
        summary=f"{current_user.email} changed a staff attendance record for {attendance.staff_id} from {old_status} to {body.status}",
        old_values={"status": old_status}, new_values={"status": body.status},
    )

    return {"message": "Staff attendance updated"}
