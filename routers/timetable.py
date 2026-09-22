"""Timetable router"""
from fastapi import APIRouter, Depends, HTTPException, status, Response
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlalchemy import and_
from datetime import datetime
from typing import Optional, List
from models.timetable import Timetable, TimetableCreate, Period, PeriodCreate, DayOfWeek, PeriodType
from models.classroom import Class, Subject
from models.staff import Staff, TeacherAssignment
from models.school import AcademicTerm
from models.facilities import FacilityRoom, FacilityBooking
from models.user import User, UserRole
from database import get_session
from auth import get_current_user, require_roles
from dependencies import assert_campus_access
from services.timetable_pdf_service import TimetablePDFService
from services.plan_gating import require_plan_feature

router = APIRouter(prefix="/timetable", tags=["Timetable"])

_WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


async def validate_facility_room(session: AsyncSession, school_id: str, facility_room_id: str) -> None:
    """Mirrors routers/facilities.py's _related() helper — validates a
    facility_room_id points at a real, same-school FacilityRoom."""
    result = await session.execute(select(FacilityRoom).where(FacilityRoom.id == facility_room_id, FacilityRoom.school_id == school_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="facility_room_id does not exist for this school")


async def check_facility_room_conflict(
    session: AsyncSession, school_id: str, facility_room_id: str, day_of_week, period: Optional[Period],
    academic_term_id: str, exclude_class_id: Optional[str] = None,
) -> None:
    """Additive alongside the existing free-text Timetable.room same-string
    check above — validates a bookable facility_room_id two ways:
      1. Another recurring Timetable slot already uses this room at the same
         day/period/term (same domain, same shape as the existing room check).
      2. A one-off FacilityBooking already reserves this room during an
         overlapping window. FacilityBooking rows carry an absolute
         start_at/end_at (see routers/facilities.py's create_booking, whose
         overlap-query shape this reuses) rather than a day-of-week + period,
         so a booking is mapped onto this weekly slot by matching weekday +
         clock-time overlap against the period's start_time/end_time; a
         start_at/end_at that isn't a parseable ISO datetime is skipped
         rather than erroring, since that field is a free-text str with no
         format validation of its own.
    """
    query = select(Timetable).where(
        Timetable.school_id == school_id,
        Timetable.facility_room_id == facility_room_id,
        Timetable.period_id == (period.id if period else None),
        Timetable.day_of_week == day_of_week,
        Timetable.academic_term_id == academic_term_id,
    )
    if exclude_class_id:
        query = query.where(Timetable.class_id != exclude_class_id)
    existing = await session.execute(query)
    if existing.first():
        raise HTTPException(status_code=400, detail="This bookable room is already scheduled for another class in this period")

    if not period or not period.start_time or not period.end_time:
        return
    try:
        period_start = datetime.strptime(period.start_time, "%H:%M").time()
        period_end = datetime.strptime(period.end_time, "%H:%M").time()
    except (ValueError, TypeError):
        return

    day_name = day_of_week.value if hasattr(day_of_week, "value") else str(day_of_week)
    bookings_result = await session.execute(
        select(FacilityBooking).where(
            FacilityBooking.school_id == school_id,
            FacilityBooking.room_id == facility_room_id,
            FacilityBooking.status != "cancelled",
        )
    )
    for booking in bookings_result.scalars().all():
        try:
            booking_start = datetime.fromisoformat(booking.start_at)
            booking_end = datetime.fromisoformat(booking.end_at)
        except (ValueError, TypeError):
            continue
        if _WEEKDAY_NAMES[booking_start.weekday()] != day_name:
            continue
        if booking_start.time() < period_end and booking_end.time() > period_start:
            raise HTTPException(
                status_code=409,
                detail=f"This room has an existing facility booking ('{booking.title}') that overlaps this period"
            )


async def get_current_term_id(session: AsyncSession, school_id: str) -> Optional[str]:
    """Resolve the school's actual current term instead of the old hardcoded
    'term_1_2026' fallback that every timetable endpoint used to default to."""
    result = await session.execute(
        select(AcademicTerm).where(AcademicTerm.school_id == school_id, AcademicTerm.is_current == True)
    )
    term = result.scalar_one_or_none()
    return term.id if term else None


async def validate_timetable_references(
    session: AsyncSession, school_id: str, class_id: str, subject_id: str,
    teacher_id: str, period_id: str, academic_term_id: str, current_user: User
) -> None:
    """None of class_id/subject_id/teacher_id/period_id had a foreign key, so a
    bad or cross-tenant id was previously accepted silently and rendered as
    'Unknown' everywhere the schedule is displayed."""
    class_result = await session.execute(select(Class).where(Class.id == class_id, Class.school_id == school_id))
    cls = class_result.scalar_one_or_none()
    if not cls:
        raise HTTPException(status_code=400, detail="class_id does not exist for this school")
    # timetable.py previously had zero campus scoping anywhere -- a
    # campus-scoped SCHOOL_ADMIN could schedule entries for a class in a
    # different campus of the same school.
    assert_campus_access(current_user, cls.campus_id)

    subject_result = await session.execute(select(Subject).where(Subject.id == subject_id, Subject.school_id == school_id))
    if not subject_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="subject_id does not exist for this school")

    teacher_result = await session.execute(select(Staff).where(Staff.id == teacher_id, Staff.school_id == school_id))
    if not teacher_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="teacher_id does not exist for this school")

    assignment_result = await session.execute(select(TeacherAssignment).where(
        TeacherAssignment.school_id == school_id,
        TeacherAssignment.staff_id == teacher_id,
        TeacherAssignment.class_id == class_id,
        TeacherAssignment.subject_id == subject_id,
        TeacherAssignment.academic_term_id == academic_term_id,
    ))
    if not assignment_result.scalar_one_or_none():
        # This is a data-integrity check, not an actor-authorization one —
        # only SUPER_ADMIN/SCHOOL_ADMIN can even reach create_timetable_entry
        # (require_roles above), so it's not "is this caller allowed", it's
        # "does the referenced teacher_id actually teach this class/subject".
        # Without it, an admin could schedule a teacher into a class they've
        # never been assigned to — routers/teacher/timetable.py's own
        # TeacherAssignment-scoped queries (my-schedule, class timetable
        # view) would then never surface that entry to the teacher at all,
        # even though it appears in the admin's own schedule view.
        raise HTTPException(status_code=400, detail="This teacher is not assigned to teach this class/subject for this term")

    period_result = await session.execute(
        select(Period).where(Period.id == period_id, Period.school_id == school_id, Period.is_active == True)
    )
    if not period_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="period_id does not exist or is inactive for this school")

    term_result = await session.execute(
        select(AcademicTerm).where(AcademicTerm.id == academic_term_id, AcademicTerm.school_id == school_id)
    )
    if not term_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="academic_term_id does not exist for this school")


async def build_class_schedule(session: AsyncSession, school_id: str, class_id: str, academic_term_id: str, current_user: User) -> dict:
    """Shared by the JSON and PDF class-timetable endpoints so the school-scoping
    check and schedule-building logic only exist in one place."""
    class_result = await session.execute(select(Class).where(Class.id == class_id, Class.school_id == school_id))
    cls = class_result.scalar_one_or_none()
    if not cls:
        raise HTTPException(status_code=404, detail="Class not found")
    assert_campus_access(current_user, cls.campus_id)

    periods_result = await session.execute(
        select(Period).where(Period.school_id == school_id, Period.is_active == True).order_by(Period.period_number)
    )
    periods = {p.id: p for p in periods_result.scalars().all()}

    result = await session.execute(
        select(Timetable).where(Timetable.class_id == class_id, Timetable.academic_term_id == academic_term_id)
    )
    entries = result.scalars().all()

    subject_ids = list(set(e.subject_id for e in entries))
    teacher_ids = list(set(e.teacher_id for e in entries))

    subjects = {}
    if subject_ids:
        subject_result = await session.execute(select(Subject).where(Subject.id.in_(subject_ids)))
        subjects = {s.id: s for s in subject_result.scalars().all()}

    teachers = {}
    if teacher_ids:
        teacher_result = await session.execute(select(Staff).where(Staff.id.in_(teacher_ids)))
        teachers = {t.id: t for t in teacher_result.scalars().all()}

    schedule = {day.value: [] for day in DayOfWeek}
    for entry in entries:
        period = periods.get(entry.period_id)
        subject = subjects.get(entry.subject_id)
        teacher = teachers.get(entry.teacher_id)
        schedule[entry.day_of_week.value].append({
            "id": entry.id,
            "period_id": entry.period_id,
            "period_name": period.name if period else "Unknown",
            "period_number": period.period_number if period else 0,
            "start_time": period.start_time if period else "",
            "end_time": period.end_time if period else "",
            "subject_id": entry.subject_id,
            "subject_name": subject.name if subject else "Unknown",
            "teacher_id": entry.teacher_id,
            "teacher_name": f"{teacher.first_name} {teacher.last_name}" if teacher else "Unknown",
            "room": entry.room,
            "facility_room_id": entry.facility_room_id
        })
    for day in schedule:
        schedule[day].sort(key=lambda x: x["period_number"])

    return {
        "class_id": class_id,
        "class_name": cls.name,
        "academic_term_id": academic_term_id,
        "periods": [
            {
                "id": p.id, "name": p.name, "period_number": p.period_number,
                "start_time": p.start_time, "end_time": p.end_time, "period_type": p.period_type
            }
            for p in sorted(periods.values(), key=lambda x: x.period_number)
        ],
        "schedule": schedule
    }


async def build_teacher_schedule(session: AsyncSession, school_id: str, teacher_id: str, academic_term_id: str) -> dict:
    """Shared by the JSON and PDF teacher-timetable endpoints — see build_class_schedule."""
    teacher_result = await session.execute(select(Staff).where(Staff.id == teacher_id, Staff.school_id == school_id))
    teacher = teacher_result.scalar_one_or_none()
    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher not found")

    periods_result = await session.execute(
        select(Period).where(Period.school_id == school_id, Period.is_active == True).order_by(Period.period_number)
    )
    periods = {p.id: p for p in periods_result.scalars().all()}

    result = await session.execute(
        select(Timetable).where(Timetable.teacher_id == teacher_id, Timetable.academic_term_id == academic_term_id)
    )
    entries = result.scalars().all()

    subject_ids = list(set(e.subject_id for e in entries))
    class_ids = list(set(e.class_id for e in entries))

    subjects = {}
    if subject_ids:
        subject_result = await session.execute(select(Subject).where(Subject.id.in_(subject_ids)))
        subjects = {s.id: s for s in subject_result.scalars().all()}

    classes = {}
    if class_ids:
        class_result = await session.execute(select(Class).where(Class.id.in_(class_ids)))
        classes = {c.id: c for c in class_result.scalars().all()}

    schedule = {day.value: [] for day in DayOfWeek}
    for entry in entries:
        period = periods.get(entry.period_id)
        subject = subjects.get(entry.subject_id)
        cls = classes.get(entry.class_id)
        schedule[entry.day_of_week.value].append({
            "id": entry.id,
            "period_id": entry.period_id,
            "period_name": period.name if period else "Unknown",
            "period_number": period.period_number if period else 0,
            "start_time": period.start_time if period else "",
            "end_time": period.end_time if period else "",
            "subject_id": entry.subject_id,
            "subject_name": subject.name if subject else "Unknown",
            "class_id": entry.class_id,
            "class_name": cls.name if cls else "Unknown",
            "room": entry.room,
            "facility_room_id": entry.facility_room_id
        })
    for day in schedule:
        schedule[day].sort(key=lambda x: x["period_number"])

    return {
        "teacher_id": teacher_id,
        "teacher_name": f"{teacher.first_name} {teacher.last_name}",
        "academic_term_id": academic_term_id,
        "periods": [
            {
                "id": p.id, "name": p.name, "period_number": p.period_number,
                "start_time": p.start_time, "end_time": p.end_time, "period_type": p.period_type
            }
            for p in sorted(periods.values(), key=lambda x: x.period_number)
        ],
        "schedule": schedule,
        "total_periods_per_week": len(entries)
    }

# Default Ghana school periods
DEFAULT_PERIODS = [
    {"name": "Assembly", "period_number": 0, "start_time": "07:30", "end_time": "08:00", "period_type": "assembly"},
    {"name": "Period 1", "period_number": 1, "start_time": "08:00", "end_time": "08:40", "period_type": "lesson"},
    {"name": "Period 2", "period_number": 2, "start_time": "08:40", "end_time": "09:20", "period_type": "lesson"},
    {"name": "Period 3", "period_number": 3, "start_time": "09:20", "end_time": "10:00", "period_type": "lesson"},
    {"name": "Break", "period_number": 4, "start_time": "10:00", "end_time": "10:30", "period_type": "break"},
    {"name": "Period 4", "period_number": 5, "start_time": "10:30", "end_time": "11:10", "period_type": "lesson"},
    {"name": "Period 5", "period_number": 6, "start_time": "11:10", "end_time": "11:50", "period_type": "lesson"},
    {"name": "Period 6", "period_number": 7, "start_time": "11:50", "end_time": "12:30", "period_type": "lesson"},
    {"name": "Lunch", "period_number": 8, "start_time": "12:30", "end_time": "13:30", "period_type": "lunch"},
    {"name": "Period 7", "period_number": 9, "start_time": "13:30", "end_time": "14:10", "period_type": "lesson"},
    {"name": "Period 8", "period_number": 10, "start_time": "14:10", "end_time": "14:50", "period_type": "lesson"},
]


@router.post("/periods/seed-defaults", response_model=dict)
async def seed_default_periods(
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session)
):
    """Seed default Ghana school periods"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    # Check if periods already exist
    existing = await session.execute(
        select(Period).where(Period.school_id == school_id).limit(1)
    )
    if existing.scalar_one_or_none():
        return {"message": "Periods already exist", "created": 0}
    
    created_count = 0
    for p in DEFAULT_PERIODS:
        period = Period(
            school_id=school_id,
            name=p["name"],
            period_number=p["period_number"],
            start_time=p["start_time"],
            end_time=p["end_time"],
            period_type=PeriodType(p["period_type"])
        )
        session.add(period)
        created_count += 1
    
    await session.commit()
    return {"message": f"Created {created_count} default periods", "created": created_count}


@router.post("/periods", response_model=dict)
async def create_period(
    period_data: PeriodCreate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session)
):
    """Create a period definition"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    period = Period(school_id=school_id, **period_data.model_dump())
    session.add(period)
    await session.commit()
    await session.refresh(period)
    
    return {
        "id": period.id,
        "name": period.name,
        "period_number": period.period_number,
        "start_time": period.start_time,
        "end_time": period.end_time,
        "period_type": period.period_type,
        "message": "Period created"
    }


@router.patch("/periods/{period_id}", response_model=dict)
async def update_period(
    period_id: str,
    period_data: PeriodCreate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session)
):
    """Update a period definition"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(Period).where(
            and_(Period.id == period_id, Period.school_id == school_id, Period.is_active == True)
        )
    )
    period = result.scalar_one_or_none()
    if not period:
        raise HTTPException(status_code=404, detail="Period not found")

    period.name = period_data.name
    period.period_number = period_data.period_number
    period.start_time = period_data.start_time
    period.end_time = period_data.end_time
    period.period_type = period_data.period_type

    session.add(period)
    await session.commit()
    await session.refresh(period)

    return {
        "id": period.id,
        "name": period.name,
        "period_number": period.period_number,
        "start_time": period.start_time,
        "end_time": period.end_time,
        "period_type": period.period_type,
        "message": "Period updated"
    }


@router.delete("/periods/{period_id}", response_model=dict)
async def delete_period(
    period_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session)
):
    """Deactivate a period definition"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(Period).where(
            and_(Period.id == period_id, Period.school_id == school_id, Period.is_active == True)
        )
    )
    period = result.scalar_one_or_none()
    if not period:
        raise HTTPException(status_code=404, detail="Period not found")

    # Deactivating a period used to leave any Timetable entry that referenced it
    # silently rendering as "Unknown" / sorting to the top of the day. Block the
    # deactivation instead — the admin needs to reassign or remove those
    # entries first, same as you can't delete a class subjects still point to.
    in_use = await session.execute(
        select(Timetable.id).where(Timetable.period_id == period_id).limit(1)
    )
    if in_use.scalar_one_or_none():
        raise HTTPException(
            status_code=400,
            detail="This period is still used by one or more timetable entries — remove or reassign them first"
        )

    period.is_active = False
    session.add(period)
    await session.commit()

    return {"message": "Period deleted"}


@router.get("/periods", response_model=list[dict])
async def list_periods(
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session)
):
    """List all period definitions"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(Period).where(
            Period.school_id == school_id,
            Period.is_active == True
        ).order_by(Period.period_number)
    )
    periods = result.scalars().all()
    
    return [
        {
            "id": p.id,
            "name": p.name,
            "period_number": p.period_number,
            "start_time": p.start_time,
            "end_time": p.end_time,
            "period_type": p.period_type
        }
        for p in periods
    ]


@router.post("", response_model=dict)
async def create_timetable_entry(
    timetable_data: TimetableCreate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session)
):
    """Create a timetable entry"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    await validate_timetable_references(
        session, school_id, timetable_data.class_id, timetable_data.subject_id,
        timetable_data.teacher_id, timetable_data.period_id, timetable_data.academic_term_id,
        current_user,
    )

    existing = await session.execute(
        select(Timetable).where(
            Timetable.school_id == school_id,
            Timetable.class_id == timetable_data.class_id,
            Timetable.period_id == timetable_data.period_id,
            Timetable.day_of_week == timetable_data.day_of_week,
            Timetable.academic_term_id == timetable_data.academic_term_id
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Time slot already occupied for this class")

    teacher_conflict = await session.execute(
        select(Timetable).where(
            Timetable.school_id == school_id,
            Timetable.teacher_id == timetable_data.teacher_id,
            Timetable.period_id == timetable_data.period_id,
            Timetable.day_of_week == timetable_data.day_of_week,
            Timetable.academic_term_id == timetable_data.academic_term_id
        )
    )
    if teacher_conflict.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="This teacher is already scheduled in another class for this period")

    if timetable_data.room:
        room_conflict = await session.execute(
            select(Timetable).where(
                Timetable.school_id == school_id,
                Timetable.room == timetable_data.room,
                Timetable.period_id == timetable_data.period_id,
                Timetable.day_of_week == timetable_data.day_of_week,
                Timetable.academic_term_id == timetable_data.academic_term_id,
                Timetable.class_id != timetable_data.class_id
            )
        )
        # .scalars().first() rather than .scalar_one_or_none() — unlike the
        # slot/teacher checks above, this one has no backing DB unique
        # constraint (the class_id != exclusion here doesn't map cleanly
        # onto a plain unique index), so more than one match is possible in
        # principle and shouldn't crash the request.
        if room_conflict.scalars().first():
            raise HTTPException(status_code=400, detail=f"Room '{timetable_data.room}' is already booked for another class in this period")

    if timetable_data.facility_room_id:
        await validate_facility_room(session, school_id, timetable_data.facility_room_id)
        period_result = await session.execute(select(Period).where(Period.id == timetable_data.period_id))
        period_obj = period_result.scalar_one_or_none()
        await check_facility_room_conflict(
            session, school_id, timetable_data.facility_room_id, timetable_data.day_of_week,
            period_obj, timetable_data.academic_term_id, exclude_class_id=timetable_data.class_id,
        )

    timetable = Timetable(school_id=school_id, **timetable_data.model_dump())
    session.add(timetable)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="This slot was just booked by someone else — please retry")
    await session.refresh(timetable)

    return {
        "id": timetable.id,
        "class_id": timetable.class_id,
        "subject_id": timetable.subject_id,
        "teacher_id": timetable.teacher_id,
        "day_of_week": timetable.day_of_week,
        "message": "Timetable entry created"
    }


@router.get("/class/{class_id}", response_model=dict)
async def get_class_timetable(
    class_id: str,
    academic_term_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session)
):
    """Get weekly timetable for a class. academic_term_id defaults to the
    school's current term if not supplied."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    term_id = academic_term_id or await get_current_term_id(session, school_id)
    if not term_id:
        raise HTTPException(status_code=400, detail="No academic_term_id provided and no current term is set for this school")

    return await build_class_schedule(session, school_id, class_id, term_id, current_user)


@router.delete("/{entry_id}", response_model=dict)
async def delete_timetable_entry(
    entry_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session)
):
    """Delete a timetable entry"""
    result = await session.execute(select(Timetable).where(Timetable.id == entry_id))
    entry = result.scalar_one_or_none()
    
    if not entry:
        raise HTTPException(status_code=404, detail="Timetable entry not found")
    
    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != entry.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    # Timetable carries no campus_id of its own -- check via the Class it links.
    class_result = await session.execute(select(Class).where(Class.id == entry.class_id))
    cls = class_result.scalar_one_or_none()
    if cls:
        assert_campus_access(current_user, cls.campus_id)

    await session.delete(entry)
    await session.commit()
    
    return {"message": "Timetable entry deleted"}



@router.get("/teacher/{teacher_id}", response_model=dict)
async def get_teacher_timetable(
    teacher_id: str,
    academic_term_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session)
):
    """Get weekly timetable for a teacher. academic_term_id defaults to the
    school's current term if not supplied."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    term_id = academic_term_id or await get_current_term_id(session, school_id)
    if not term_id:
        raise HTTPException(status_code=400, detail="No academic_term_id provided and no current term is set for this school")

    return await build_teacher_schedule(session, school_id, teacher_id, term_id)


@router.get("/my-schedule", response_model=dict)
async def get_my_timetable(
    academic_term_id: Optional[str] = None,
    current_user: User = Depends(require_roles(UserRole.TEACHER)),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session)
):
    """Get weekly timetable for the current teacher. Used to compare
    Timetable.teacher_id (which stores Staff.id) against current_user.id (a
    User.id) directly — always a mismatch, so this always returned an empty
    schedule. Now resolves the caller's Staff record first, matching the
    pattern used everywhere else a logged-in teacher needs their own Staff row."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    staff_result = await session.execute(select(Staff).where(Staff.user_id == current_user.id))
    staff = staff_result.scalar_one_or_none()
    if not staff:
        raise HTTPException(status_code=404, detail="No staff record found for this account")

    term_id = academic_term_id or await get_current_term_id(session, school_id)
    if not term_id:
        raise HTTPException(status_code=400, detail="No academic_term_id provided and no current term is set for this school")

    schedule_data = await build_teacher_schedule(session, school_id, staff.id, term_id)
    return {
        "timetable": schedule_data["schedule"],
        "message": "success" if any(schedule_data["schedule"].values()) else "No classes assigned"
    }


@router.post("/bulk", response_model=dict)
async def create_bulk_timetable_entries(
    entries: List[TimetableCreate],
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session)
):
    """Create multiple timetable entries at once"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    # Batch-validate referenced ids rather than one query per field per entry.
    if entries:
        class_ids = {e.class_id for e in entries}
        subject_ids = {e.subject_id for e in entries}
        teacher_ids = {e.teacher_id for e in entries}
        period_ids = {e.period_id for e in entries}
        term_ids = {e.academic_term_id for e in entries}

        class_rows = (await session.execute(
            select(Class.id, Class.campus_id).where(Class.id.in_(class_ids), Class.school_id == school_id)
        )).all()
        found_classes = {row[0] for row in class_rows}
        if found_classes != class_ids:
            raise HTTPException(status_code=400, detail="One or more class_id values do not exist for this school")
        # timetable.py previously had zero campus scoping anywhere -- a
        # campus-scoped SCHOOL_ADMIN could bulk-schedule entries for classes
        # in a different campus of the same school.
        for _, campus_id in class_rows:
            assert_campus_access(current_user, campus_id)

        found_subjects = set((await session.execute(
            select(Subject.id).where(Subject.id.in_(subject_ids), Subject.school_id == school_id)
        )).scalars().all())
        if found_subjects != subject_ids:
            raise HTTPException(status_code=400, detail="One or more subject_id values do not exist for this school")

        found_teachers = set((await session.execute(
            select(Staff.id).where(Staff.id.in_(teacher_ids), Staff.school_id == school_id)
        )).scalars().all())
        if found_teachers != teacher_ids:
            raise HTTPException(status_code=400, detail="One or more teacher_id values do not exist for this school")

        found_periods = set((await session.execute(
            select(Period.id).where(Period.id.in_(period_ids), Period.school_id == school_id, Period.is_active == True)
        )).scalars().all())
        if found_periods != period_ids:
            raise HTTPException(status_code=400, detail="One or more period_id values do not exist or are inactive for this school")

        found_terms = set((await session.execute(
            select(AcademicTerm.id).where(AcademicTerm.id.in_(term_ids), AcademicTerm.school_id == school_id)
        )).scalars().all())
        if found_terms != term_ids:
            raise HTTPException(status_code=400, detail="One or more academic_term_id values do not exist for this school")

        # Same data-integrity check as validate_timetable_references (used
        # by the single-entry endpoint) — every (teacher, class, subject,
        # term) an entry references must be a real TeacherAssignment.
        assigned_tuples = set((await session.execute(
            select(TeacherAssignment.staff_id, TeacherAssignment.class_id, TeacherAssignment.subject_id, TeacherAssignment.academic_term_id).where(
                TeacherAssignment.school_id == school_id,
                TeacherAssignment.staff_id.in_(teacher_ids),
                TeacherAssignment.class_id.in_(class_ids),
                TeacherAssignment.subject_id.in_(subject_ids),
                TeacherAssignment.academic_term_id.in_(term_ids),
            )
        )).all())
        for e in entries:
            if (e.teacher_id, e.class_id, e.subject_id, e.academic_term_id) not in assigned_tuples:
                raise HTTPException(
                    status_code=400,
                    detail=f"Teacher {e.teacher_id} is not assigned to teach class {e.class_id}/subject {e.subject_id} for term {e.academic_term_id}",
                )

    created_count = 0
    skipped_count = 0

    for entry_data in entries:
        # Check for existing entry
        existing = await session.execute(
            select(Timetable).where(
                Timetable.school_id == school_id,
                Timetable.class_id == entry_data.class_id,
                Timetable.period_id == entry_data.period_id,
                Timetable.day_of_week == entry_data.day_of_week,
                Timetable.academic_term_id == entry_data.academic_term_id
            )
        )
        if existing.scalar_one_or_none():
            skipped_count += 1
            continue

        teacher_conflict = await session.execute(
            select(Timetable).where(
                Timetable.school_id == school_id,
                Timetable.teacher_id == entry_data.teacher_id,
                Timetable.period_id == entry_data.period_id,
                Timetable.day_of_week == entry_data.day_of_week,
                Timetable.academic_term_id == entry_data.academic_term_id
            )
        )
        if teacher_conflict.scalar_one_or_none():
            skipped_count += 1
            continue

        if entry_data.room:
            room_conflict = await session.execute(
                select(Timetable).where(
                    Timetable.school_id == school_id,
                    Timetable.room == entry_data.room,
                    Timetable.period_id == entry_data.period_id,
                    Timetable.day_of_week == entry_data.day_of_week,
                    Timetable.academic_term_id == entry_data.academic_term_id,
                    Timetable.class_id != entry_data.class_id
                )
            )
            # .scalars().first() -- see the single-entry endpoint's identical
            # comment: no backing unique constraint for this check.
            if room_conflict.scalars().first():
                skipped_count += 1
                continue

        if entry_data.facility_room_id:
            try:
                await validate_facility_room(session, school_id, entry_data.facility_room_id)
                period_result = await session.execute(select(Period).where(Period.id == entry_data.period_id))
                period_obj = period_result.scalar_one_or_none()
                await check_facility_room_conflict(
                    session, school_id, entry_data.facility_room_id, entry_data.day_of_week,
                    period_obj, entry_data.academic_term_id, exclude_class_id=entry_data.class_id,
                )
            except HTTPException:
                skipped_count += 1
                continue

        timetable = Timetable(school_id=school_id, **entry_data.model_dump())
        session.add(timetable)
        created_count += 1

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="One of these slots was just booked by someone else — please retry"
        )

    return {
        "message": f"Created {created_count} entries, skipped {skipped_count} (already exist)",
        "created": created_count,
        "skipped": skipped_count
    }


@router.get("/days", response_model=List[str])
async def get_days_of_week():
    """Get list of school days"""
    return [day.value for day in DayOfWeek]


# PDF Export Endpoints
pdf_service = TimetablePDFService()

@router.get("/class/{class_id}/export-pdf")
async def export_class_timetable_pdf(
    class_id: str,
    academic_term_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session)
):
    """Export class timetable as PDF"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    term_id = academic_term_id or await get_current_term_id(session, school_id)
    if not term_id:
        raise HTTPException(status_code=400, detail="No academic_term_id provided and no current term is set for this school")

    # Shared with GET /timetable/class/{class_id} — same school-scoping check
    # and schedule-building logic, so a fix to one automatically applies to
    # both instead of needing to be made twice (as it previously wasn't).
    timetable_data = await build_class_schedule(session, school_id, class_id, term_id, current_user)
    cls_name = timetable_data["class_name"]
    academic_term_id = term_id

    # Format for PDF
    formatted_data = pdf_service.format_timetable_for_pdf(timetable_data)

    # Generate PDF
    pdf_bytes = pdf_service.generate_pdf(formatted_data)

    # Return PDF response
    filename = f"class_timetable_{cls_name.replace(' ', '_')}_{academic_term_id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@router.get("/teacher/{teacher_id}/export-pdf")
async def export_teacher_timetable_pdf(
    teacher_id: str,
    academic_term_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session)
):
    """Export teacher timetable as PDF"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    term_id = academic_term_id or await get_current_term_id(session, school_id)
    if not term_id:
        raise HTTPException(status_code=400, detail="No academic_term_id provided and no current term is set for this school")

    # Shared with GET /timetable/teacher/{teacher_id} — see the identical
    # comment in export_class_timetable_pdf.
    timetable_data = await build_teacher_schedule(session, school_id, teacher_id, term_id)
    teacher_first_name = timetable_data["teacher_name"].split(" ")[0]
    teacher_last_name = " ".join(timetable_data["teacher_name"].split(" ")[1:])
    academic_term_id = term_id

    # Format for PDF
    formatted_data = pdf_service.format_timetable_for_pdf(timetable_data)

    # Generate PDF
    pdf_bytes = pdf_service.generate_pdf(formatted_data)

    # Return PDF response
    filename = f"teacher_timetable_{teacher_first_name}_{teacher_last_name}_{academic_term_id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )
