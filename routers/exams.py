"""Internal Exam Scheduling Router

Schedules the school's own exams (midterms, finals) — distinct from
routers/exam_board.py, which handles external board sittings (WAEC/BECE).
Real conflict detection throughout: a room can't host two overlapping
exams, a class can't sit two exams at once, and a staff member can't
invigilate two rooms at once.
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from sqlmodel import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from datetime import datetime
from typing import List, Optional

from models.exam import (
    ExamSession, ExamSessionCreate, ExamSessionUpdate, ExamSessionStatus, ExamSessionReleaseDate,
    ExamSchedule, ExamScheduleCreate, ExamScheduleUpdate,
    ExamSeatAssignment, ExamSeatAssignmentUpdate,
    ExamInvigilator, ExamInvigilatorCreate,
)
from models.classroom import Class
from models.student import Student
from models.staff import Staff
from models.user import User
from database import get_session
from auth import get_current_user, require_permission
from dependencies import resolve_campus_scope, resolve_write_campus_id, assert_campus_access
from services.exam_result_aggregation_service import aggregate_exam_session_to_grades, revert_exam_session_grades

router = APIRouter(prefix="/exams", tags=["Exams"])


def _times_overlap(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    """'HH:MM' 24-hour strings compare lexicographically the same as chronologically."""
    return start_a < end_b and start_b < end_a


async def _get_session_or_404(session: AsyncSession, school_id: str, exam_session_id: str) -> ExamSession:
    result = await session.execute(
        select(ExamSession).where(and_(ExamSession.id == exam_session_id, ExamSession.school_id == school_id))
    )
    exam_session = result.scalar_one_or_none()
    if not exam_session:
        raise HTTPException(status_code=404, detail="Exam session not found")
    return exam_session


async def _get_schedule_or_404(session: AsyncSession, school_id: str, schedule_id: str) -> ExamSchedule:
    result = await session.execute(
        select(ExamSchedule).where(and_(ExamSchedule.id == schedule_id, ExamSchedule.school_id == school_id))
    )
    schedule = result.scalar_one_or_none()
    if not schedule:
        raise HTTPException(status_code=404, detail="Exam schedule not found")
    return schedule


async def _assert_schedule_campus_access(session: AsyncSession, current_user: User, schedule: ExamSchedule) -> None:
    """ExamSchedule carries no campus_id of its own -- resolve via its Class.
    create_exam_schedule already checks this on the target Class; every
    other mutation on the same schedule/seating/invigilator objects
    previously skipped it entirely."""
    class_result = await session.execute(select(Class).where(Class.id == schedule.class_id))
    cls = class_result.scalar_one_or_none()
    if cls:
        assert_campus_access(current_user, cls.campus_id)


async def _check_room_conflict(
    session: AsyncSession, school_id: str, exam_date: str, room: Optional[str],
    start_time: str, end_time: str, exclude_schedule_id: Optional[str] = None,
) -> None:
    if not room:
        return
    query = select(ExamSchedule).where(
        ExamSchedule.school_id == school_id,
        ExamSchedule.exam_date == exam_date,
        ExamSchedule.room == room,
    )
    if exclude_schedule_id:
        query = query.where(ExamSchedule.id != exclude_schedule_id)
    result = await session.execute(query)
    for other in result.scalars().all():
        if _times_overlap(start_time, end_time, other.start_time, other.end_time):
            raise HTTPException(status_code=409, detail=f"Room '{room}' is already booked for an overlapping exam on {exam_date}")


async def _check_class_conflict(
    session: AsyncSession, school_id: str, class_id: str, exam_date: str,
    start_time: str, end_time: str, exclude_schedule_id: Optional[str] = None,
) -> None:
    query = select(ExamSchedule).where(
        ExamSchedule.school_id == school_id,
        ExamSchedule.class_id == class_id,
        ExamSchedule.exam_date == exam_date,
    )
    if exclude_schedule_id:
        query = query.where(ExamSchedule.id != exclude_schedule_id)
    result = await session.execute(query)
    for other in result.scalars().all():
        if _times_overlap(start_time, end_time, other.start_time, other.end_time):
            raise HTTPException(status_code=409, detail=f"This class already has an overlapping exam scheduled on {exam_date}")


# ── Sessions ─────────────────────────────────────────────────────────────

@router.post("/sessions", response_model=dict)
async def create_exam_session(
    data: ExamSessionCreate,
    current_user: User = Depends(require_permission("exams.session.manage")),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    campus_id = resolve_write_campus_id(current_user, data.campus_id)
    exam_session = ExamSession(school_id=school_id, **{**data.model_dump(), "campus_id": campus_id})
    session.add(exam_session)
    await session.commit()
    await session.refresh(exam_session)
    return jsonable_encoder(exam_session)


@router.get("/sessions", response_model=List[dict])
async def list_exam_sessions(
    academic_term_id: Optional[str] = None,
    campus_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(ExamSession).where(ExamSession.school_id == school_id)
    if academic_term_id:
        query = query.where(ExamSession.academic_term_id == academic_term_id)
    campus_id = resolve_campus_scope(current_user, campus_id)
    if campus_id:
        query = query.where(or_(ExamSession.campus_id == campus_id, ExamSession.campus_id.is_(None)))

    result = await session.execute(query.order_by(ExamSession.start_date.desc()))
    return [jsonable_encoder(s) for s in result.scalars().all()]


@router.get("/sessions/{exam_session_id}", response_model=dict)
async def get_exam_session(
    exam_session_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    exam_session = await _get_session_or_404(session, school_id, exam_session_id)
    assert_campus_access(current_user, exam_session.campus_id)

    schedules_result = await session.execute(
        select(ExamSchedule).where(ExamSchedule.exam_session_id == exam_session_id).order_by(ExamSchedule.exam_date, ExamSchedule.start_time)
    )
    schedules = schedules_result.scalars().all()

    return {**jsonable_encoder(exam_session), "schedule_count": len(schedules), "schedules": [jsonable_encoder(s) for s in schedules]}


@router.put("/sessions/{exam_session_id}", response_model=dict)
async def update_exam_session(
    exam_session_id: str,
    data: ExamSessionUpdate,
    current_user: User = Depends(require_permission("exams.session.manage")),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    exam_session = await _get_session_or_404(session, school_id, exam_session_id)
    assert_campus_access(current_user, exam_session.campus_id)

    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(exam_session, key, value)
    exam_session.updated_at = datetime.utcnow()
    session.add(exam_session)
    await session.commit()
    await session.refresh(exam_session)
    return jsonable_encoder(exam_session)


@router.delete("/sessions/{exam_session_id}", response_model=dict)
async def delete_exam_session(
    exam_session_id: str,
    current_user: User = Depends(require_permission("exams.session.manage")),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    exam_session = await _get_session_or_404(session, school_id, exam_session_id)
    assert_campus_access(current_user, exam_session.campus_id)

    # Deleting a session cascades away its ExamSchedule -> ExamComponent ->
    # ExamComponentMark rows -- the underlying evidence for any Grade rows
    # aggregate_exam_session_to_grades already wrote on publish. Without
    # this, those Grade rows are left behind, permanently unauditable/
    # un-revertable, but still counting toward the student's report card/
    # GPA. unpublish_results already has the correct, exact-tag-matched
    # reversion for this; reuse it here before the cascade destroys the
    # source data it depends on.
    grade_reversion = None
    if exam_session.results_published:
        grade_reversion = await revert_exam_session_grades(session, exam_session)

    await session.delete(exam_session)
    await session.commit()
    result = {"success": True, "message": "Exam session deleted"}
    if grade_reversion is not None:
        result["grade_reversion"] = grade_reversion
    return result


# ── Result Publication ──────────────────────────────────────────────────

@router.put("/sessions/{exam_session_id}/release-date", response_model=dict)
async def set_release_date(
    exam_session_id: str,
    data: ExamSessionReleaseDate,
    current_user: User = Depends(require_permission("exams.session.publish")),
    session: AsyncSession = Depends(get_session),
):
    """Sets a scheduled auto-publish date — services/scheduler.py's
    run_exam_result_auto_publish flips results_published on once this date
    passes, without anyone having to click Publish manually."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    exam_session = await _get_session_or_404(session, school_id, exam_session_id)
    assert_campus_access(current_user, exam_session.campus_id)

    exam_session.results_release_date = data.results_release_date
    exam_session.updated_at = datetime.utcnow()
    session.add(exam_session)
    await session.commit()
    await session.refresh(exam_session)
    return jsonable_encoder(exam_session)


@router.post("/sessions/{exam_session_id}/publish-results", response_model=dict)
async def publish_results(
    exam_session_id: str,
    current_user: User = Depends(require_permission("exams.session.publish")),
    session: AsyncSession = Depends(get_session),
):
    """Manual override — makes every component mark under this session
    visible to students/parents immediately, regardless of results_release_date.
    Also aggregates each student's component marks into the gradebook (see
    services/exam_result_aggregation_service) so the exam counts toward
    report cards/GPA, not just the visible-marks view."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    exam_session = await _get_session_or_404(session, school_id, exam_session_id)
    assert_campus_access(current_user, exam_session.campus_id)

    exam_session.results_published = True
    exam_session.results_published_by = current_user.id
    exam_session.results_published_at = datetime.utcnow()
    exam_session.updated_at = datetime.utcnow()
    session.add(exam_session)
    await session.commit()
    await session.refresh(exam_session)

    aggregation = await aggregate_exam_session_to_grades(session, exam_session, actor_id=current_user.id)

    result = jsonable_encoder(exam_session)
    result["grade_aggregation"] = aggregation
    return result


@router.post("/sessions/{exam_session_id}/unpublish-results", response_model=dict)
async def unpublish_results(
    exam_session_id: str,
    current_user: User = Depends(require_permission("exams.session.publish")),
    session: AsyncSession = Depends(get_session),
):
    """Pulls results back out of view — e.g. a marking error was found
    after publishing. Does not touch results_release_date, so an auto-publish
    sweep would otherwise re-publish it again the same day; callers who need
    that also cleared should call the release-date endpoint separately.

    Also reverts the Grade rows the earlier publish auto-aggregated (see
    services/exam_result_aggregation_service.revert_exam_session_grades) —
    without this, a student's report card/GPA kept counting a session's
    results even after it was pulled back for being wrong."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    exam_session = await _get_session_or_404(session, school_id, exam_session_id)
    assert_campus_access(current_user, exam_session.campus_id)

    exam_session.results_published = False
    exam_session.results_published_by = None
    exam_session.results_published_at = None
    exam_session.updated_at = datetime.utcnow()
    session.add(exam_session)
    await session.commit()
    await session.refresh(exam_session)

    reversion = await revert_exam_session_grades(session, exam_session)

    result = jsonable_encoder(exam_session)
    result["grade_reversion"] = reversion
    return result


# ── Schedules ────────────────────────────────────────────────────────────

@router.post("/sessions/{exam_session_id}/schedules", response_model=dict)
async def create_exam_schedule(
    exam_session_id: str,
    data: ExamScheduleCreate,
    current_user: User = Depends(require_permission("exams.schedule.manage")),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    exam_session = await _get_session_or_404(session, school_id, exam_session_id)
    assert_campus_access(current_user, exam_session.campus_id)

    class_result = await session.execute(select(Class).where(and_(Class.id == data.class_id, Class.school_id == school_id)))
    target_class = class_result.scalar_one_or_none()
    if not target_class:
        raise HTTPException(status_code=404, detail="Class not found")
    assert_campus_access(current_user, target_class.campus_id)

    if data.start_time >= data.end_time:
        raise HTTPException(status_code=400, detail="start_time must be before end_time")

    await _check_room_conflict(session, school_id, data.exam_date, data.room, data.start_time, data.end_time)
    await _check_class_conflict(session, school_id, data.class_id, data.exam_date, data.start_time, data.end_time)

    schedule = ExamSchedule(school_id=school_id, exam_session_id=exam_session_id, **data.model_dump())
    session.add(schedule)
    await session.commit()
    await session.refresh(schedule)
    return jsonable_encoder(schedule)


@router.put("/schedules/{schedule_id}", response_model=dict)
async def update_exam_schedule(
    schedule_id: str,
    data: ExamScheduleUpdate,
    current_user: User = Depends(require_permission("exams.schedule.manage")),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    schedule = await _get_schedule_or_404(session, school_id, schedule_id)
    await _assert_schedule_campus_access(session, current_user, schedule)

    update_data = data.model_dump(exclude_unset=True)
    new_date = update_data.get("exam_date", schedule.exam_date)
    new_start = update_data.get("start_time", schedule.start_time)
    new_end = update_data.get("end_time", schedule.end_time)
    new_room = update_data.get("room", schedule.room)
    if new_start >= new_end:
        raise HTTPException(status_code=400, detail="start_time must be before end_time")

    await _check_room_conflict(session, school_id, new_date, new_room, new_start, new_end, exclude_schedule_id=schedule_id)
    await _check_class_conflict(session, school_id, schedule.class_id, new_date, new_start, new_end, exclude_schedule_id=schedule_id)

    for key, value in update_data.items():
        setattr(schedule, key, value)
    schedule.updated_at = datetime.utcnow()
    session.add(schedule)
    await session.commit()
    await session.refresh(schedule)
    return jsonable_encoder(schedule)


@router.delete("/schedules/{schedule_id}", response_model=dict)
async def delete_exam_schedule(
    schedule_id: str,
    current_user: User = Depends(require_permission("exams.schedule.manage")),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    schedule = await _get_schedule_or_404(session, school_id, schedule_id)
    await _assert_schedule_campus_access(session, current_user, schedule)

    # Unlike a whole session (delete_exam_session, which can call
    # revert_exam_session_grades), there's no per-schedule grade-reversion
    # helper -- that function reverts every class+subject aggregated under
    # the session, not just this one schedule's. Rather than build a new,
    # narrower reversion path, block the delete once results are published
    # and point the caller at the existing, correct workflow: unpublish
    # first (which reverts the aggregated Grade rows), then delete.
    exam_session = await session.get(ExamSession, schedule.exam_session_id)
    if exam_session and exam_session.results_published:
        raise HTTPException(
            status_code=400,
            detail="This exam's results have already been published — unpublish the exam session first so its aggregated grades are reverted, then delete the schedule.",
        )

    await session.delete(schedule)
    await session.commit()
    return {"success": True, "message": "Exam schedule deleted"}


# ── Seating ──────────────────────────────────────────────────────────────

@router.post("/schedules/{schedule_id}/generate-seating", response_model=dict)
async def generate_seating(
    schedule_id: str,
    current_user: User = Depends(require_permission("exams.seating.manage")),
    session: AsyncSession = Depends(get_session),
):
    """(Re)generates seat numbers for every active student in the schedule's
    class, in the schedule's room. Idempotent — clears any existing
    assignments for this schedule first, so re-running after a class-list
    change just re-numbers cleanly rather than piling up stale rows."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    schedule = await _get_schedule_or_404(session, school_id, schedule_id)
    await _assert_schedule_campus_access(session, current_user, schedule)
    if not schedule.room:
        raise HTTPException(status_code=400, detail="Assign a room to this exam schedule before generating seating")

    students_result = await session.execute(
        select(Student).where(
            Student.class_id == schedule.class_id,
            Student.school_id == school_id,
            Student.status == "active",
        ).order_by(Student.first_name, Student.last_name)
    )
    students = students_result.scalars().all()
    if not students:
        raise HTTPException(status_code=404, detail="No active students in this class")

    # A student under an active (APPROVED, not yet cleared) suspension is
    # not eligible to sit an exam until a staff member clears it — see
    # routers/discipline.py::clear_suspension.
    from routers.discipline import get_suspended_student_ids  # deferred: avoids import cycle
    suspended_ids = await get_suspended_student_ids(session, school_id, [s.id for s in students])
    eligible_students = [s for s in students if s.id not in suspended_ids]
    if not eligible_students:
        raise HTTPException(status_code=400, detail="No eligible students in this class — all active students are currently suspended")

    existing_result = await session.execute(
        select(ExamSeatAssignment).where(ExamSeatAssignment.exam_schedule_id == schedule_id)
    )
    for existing in existing_result.scalars().all():
        await session.delete(existing)
    await session.flush()

    assignments = []
    for i, student in enumerate(eligible_students, start=1):
        assignment = ExamSeatAssignment(
            school_id=school_id, exam_schedule_id=schedule_id, student_id=student.id,
            room=schedule.room, seat_number=str(i),
        )
        session.add(assignment)
        assignments.append(assignment)

    await session.commit()
    for a in assignments:
        await session.refresh(a)

    return {
        "success": True,
        "seats_assigned": len(assignments),
        "room": schedule.room,
        "excluded_suspended_count": len(suspended_ids),
        "assignments": [jsonable_encoder(a) for a in assignments],
    }


@router.get("/schedules/{schedule_id}/seating", response_model=List[dict])
async def get_seating(
    schedule_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    await _get_schedule_or_404(session, school_id, schedule_id)

    result = await session.execute(
        select(ExamSeatAssignment).where(ExamSeatAssignment.exam_schedule_id == schedule_id)
        .order_by(ExamSeatAssignment.room, ExamSeatAssignment.seat_number)
    )
    assignments = result.scalars().all()

    student_ids = [a.student_id for a in assignments]
    students_map = {}
    if student_ids:
        students_result = await session.execute(select(Student).where(Student.id.in_(student_ids)))
        students_map = {s.id: s for s in students_result.scalars().all()}

    return [
        {
            **jsonable_encoder(a),
            "student_name": f"{students_map[a.student_id].first_name} {students_map[a.student_id].last_name}" if a.student_id in students_map else "Unknown",
            "student_id_code": students_map[a.student_id].student_id if a.student_id in students_map else None,
        }
        for a in assignments
    ]


@router.put("/seating/{seat_assignment_id}", response_model=dict)
async def update_seat_assignment(
    seat_assignment_id: str,
    data: ExamSeatAssignmentUpdate,
    current_user: User = Depends(require_permission("exams.seating.manage")),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    result = await session.execute(
        select(ExamSeatAssignment).where(and_(ExamSeatAssignment.id == seat_assignment_id, ExamSeatAssignment.school_id == school_id))
    )
    assignment = result.scalar_one_or_none()
    if not assignment:
        raise HTTPException(status_code=404, detail="Seat assignment not found")
    schedule = await _get_schedule_or_404(session, school_id, assignment.exam_schedule_id)
    await _assert_schedule_campus_access(session, current_user, schedule)

    update_data = data.model_dump(exclude_unset=True)
    new_room = update_data.get("room", assignment.room)
    new_seat_number = update_data.get("seat_number", assignment.seat_number)
    if (new_room, new_seat_number) != (assignment.room, assignment.seat_number):
        collision = (await session.execute(
            select(ExamSeatAssignment).where(
                ExamSeatAssignment.exam_schedule_id == assignment.exam_schedule_id,
                ExamSeatAssignment.room == new_room,
                ExamSeatAssignment.seat_number == new_seat_number,
                ExamSeatAssignment.id != assignment.id,
            )
        )).scalar_one_or_none()
        if collision:
            raise HTTPException(status_code=409, detail=f"Seat {new_seat_number} in room {new_room} is already assigned to another student for this exam")

    for key, value in update_data.items():
        setattr(assignment, key, value)
    session.add(assignment)
    await session.commit()
    await session.refresh(assignment)
    return jsonable_encoder(assignment)


# ── Invigilators ─────────────────────────────────────────────────────────

@router.post("/schedules/{schedule_id}/invigilators", response_model=dict)
async def assign_invigilator(
    schedule_id: str,
    data: ExamInvigilatorCreate,
    current_user: User = Depends(require_permission("exams.invigilator.manage")),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    schedule = await _get_schedule_or_404(session, school_id, schedule_id)
    await _assert_schedule_campus_access(session, current_user, schedule)

    staff_result = await session.execute(select(Staff).where(and_(Staff.id == data.staff_id, Staff.school_id == school_id)))
    if not staff_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Staff member not found")

    already_result = await session.execute(
        select(ExamInvigilator).where(
            ExamInvigilator.exam_schedule_id == schedule_id,
            ExamInvigilator.staff_id == data.staff_id,
        )
    )
    if already_result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="This staff member is already invigilating this exam")

    # Conflict: same staff member invigilating a DIFFERENT room/schedule at an overlapping time that day.
    other_duties_result = await session.execute(
        select(ExamInvigilator, ExamSchedule)
        .join(ExamSchedule, ExamInvigilator.exam_schedule_id == ExamSchedule.id)
        .where(
            ExamInvigilator.staff_id == data.staff_id,
            ExamSchedule.school_id == school_id,
            ExamSchedule.exam_date == schedule.exam_date,
            ExamSchedule.id != schedule_id,
        )
    )
    for _, other_schedule in other_duties_result.all():
        if _times_overlap(schedule.start_time, schedule.end_time, other_schedule.start_time, other_schedule.end_time):
            raise HTTPException(status_code=409, detail="This staff member is already invigilating another exam at an overlapping time")

    invigilator = ExamInvigilator(school_id=school_id, exam_schedule_id=schedule_id, **data.model_dump())
    session.add(invigilator)
    try:
        await session.commit()
    except IntegrityError:
        # Backstop for two concurrent "assign this invigilator" calls both
        # passing the already_result check above — uq_exam_invigilators_schedule_staff
        # catches the loser here instead of leaving a duplicate row that
        # would crash this same check on any future call for this pair.
        await session.rollback()
        raise HTTPException(status_code=409, detail="This staff member is already invigilating this exam")
    await session.refresh(invigilator)
    return jsonable_encoder(invigilator)


@router.get("/schedules/{schedule_id}/invigilators", response_model=List[dict])
async def list_invigilators(
    schedule_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    await _get_schedule_or_404(session, school_id, schedule_id)

    result = await session.execute(select(ExamInvigilator).where(ExamInvigilator.exam_schedule_id == schedule_id))
    invigilators = result.scalars().all()

    staff_ids = [i.staff_id for i in invigilators]
    staff_map = {}
    if staff_ids:
        staff_result = await session.execute(select(Staff).where(Staff.id.in_(staff_ids)))
        staff_map = {s.id: s for s in staff_result.scalars().all()}

    return [
        {
            **jsonable_encoder(i),
            "staff_name": f"{staff_map[i.staff_id].first_name} {staff_map[i.staff_id].last_name}" if i.staff_id in staff_map else "Unknown",
        }
        for i in invigilators
    ]


@router.delete("/invigilators/{invigilator_id}", response_model=dict)
async def remove_invigilator(
    invigilator_id: str,
    current_user: User = Depends(require_permission("exams.invigilator.manage")),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    result = await session.execute(
        select(ExamInvigilator).where(and_(ExamInvigilator.id == invigilator_id, ExamInvigilator.school_id == school_id))
    )
    invigilator = result.scalar_one_or_none()
    if not invigilator:
        raise HTTPException(status_code=404, detail="Invigilator assignment not found")
    schedule = await _get_schedule_or_404(session, school_id, invigilator.exam_schedule_id)
    await _assert_schedule_campus_access(session, current_user, schedule)

    await session.delete(invigilator)
    await session.commit()
    return {"success": True, "message": "Invigilator removed"}
