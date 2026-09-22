"""Academic year, calendar, and controlled rollover workflows."""
from datetime import date, datetime
from typing import Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlmodel import SQLModel, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user, require_permission
from database import get_session
from models.classroom import Class, ClassLevel, ClassSubject
from models.fee import FeeStructure
from models.grade import PromotionDecision, ReportCard
from models.school import (
    AcademicTerm, AcademicYear, AcademicYearCreate, AcademicYearStatus,
    AcademicYearUpdate, CalendarEvent, CalendarEventCreate, CalendarEventUpdate,
    CalendarFeedToken, TermType,
)
from models.staff import TeacherAssignment
from models.student import Student, StudentEnrollment, StudentStatus
from models.timetable import Timetable
from models.user import User
from routers.students import check_class_capacity, _add_to_waitlist
from services import calendar_ics_service
from services.plan_gating import require_plan_feature

router = APIRouter(prefix="/academic-calendar", tags=["Academic Calendar"])
CALENDAR_MEDIA_TYPE = "text/calendar; charset=utf-8"

# Natural grade progression, youngest to oldest — index+1 is "the next level".
# A student at the last level (JHS_3) who is promoted graduates instead.
LEVEL_ORDER = list(ClassLevel)
LEVEL_INDEX = {level: i for i, level in enumerate(LEVEL_ORDER)}


class RolloverTerm(SQLModel):
    term: TermType
    start_date: str
    end_date: str


class RolloverRequest(SQLModel):
    name: str
    start_date: str
    end_date: str
    terms: list[RolloverTerm]
    carry_students: bool = True
    clone_classes: bool = True
    clone_timetable: bool = True
    clone_fee_structures: bool = True


def _school_scope(current_user: User) -> str:
    if not current_user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return current_user.school_id


def _valid_dates(start_date: str, end_date: str) -> None:
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Dates must use YYYY-MM-DD format") from exc
    if end < start:
        raise HTTPException(status_code=400, detail="End date cannot be before start date")


def _year_dict(year: AcademicYear) -> dict:
    return {
        "id": year.id, "school_id": year.school_id, "name": year.name,
        "start_date": year.start_date, "end_date": year.end_date,
        "status": year.status.value if hasattr(year.status, "value") else year.status,
        "is_current": year.is_current,
    }


def _event_dict(event: CalendarEvent) -> dict:
    return {
        "id": event.id, "school_id": event.school_id, "academic_year_id": event.academic_year_id,
        "title": event.title, "event_type": event.event_type,
        "start_date": event.start_date, "end_date": event.end_date,
        "description": event.description, "is_instructional": event.is_instructional,
    }


async def _lock_terms_for_years(session: AsyncSession, school_id: str, year_ids: list[str]) -> None:
    """Lock every AcademicTerm under the given (now-closed) years. Called any
    time a year stops being current — whether that happens via rollover or
    by directly creating/marking another year current — so historical
    locking isn't something you can accidentally bypass by not running the
    rollover wizard."""
    if not year_ids:
        return
    terms = await session.execute(select(AcademicTerm).where(AcademicTerm.school_id == school_id, AcademicTerm.academic_year_id.in_(year_ids)))
    for term in terms.scalars().all():
        term.is_locked = True


@router.get("/years", response_model=list[dict])
async def list_academic_years(
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_scope(current_user)
    result = await session.execute(select(AcademicYear).where(AcademicYear.school_id == school_id).order_by(AcademicYear.start_date.desc()))
    return [_year_dict(year) for year in result.scalars().all()]


@router.post("/years", response_model=dict)
async def create_academic_year(
    payload: AcademicYearCreate,
    current_user: User = Depends(require_permission("academics.calendar_year.manage")),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_scope(current_user)
    _valid_dates(payload.start_date, payload.end_date)
    existing = await session.execute(select(AcademicYear).where(AcademicYear.school_id == school_id, AcademicYear.name == payload.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="An academic year with this name already exists")
    if payload.is_current:
        current = await session.execute(select(AcademicYear).where(AcademicYear.school_id == school_id, AcademicYear.is_current == True))
        closing_year_ids = []
        for year in current.scalars().all():
            year.is_current = False
            if year.status == AcademicYearStatus.ACTIVE:
                year.status = AcademicYearStatus.CLOSED
            closing_year_ids.append(year.id)
        await _lock_terms_for_years(session, school_id, closing_year_ids)
    year = AcademicYear(school_id=school_id, **payload.model_dump())
    if year.is_current:
        year.status = AcademicYearStatus.ACTIVE
    session.add(year)
    await session.commit()
    await session.refresh(year)
    return _year_dict(year)


@router.put("/years/{year_id}", response_model=dict)
async def update_academic_year(
    year_id: str,
    payload: AcademicYearUpdate,
    current_user: User = Depends(require_permission("academics.calendar_year.manage")),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_scope(current_user)
    result = await session.execute(select(AcademicYear).where(AcademicYear.id == year_id, AcademicYear.school_id == school_id))
    year = result.scalar_one_or_none()
    if not year:
        raise HTTPException(status_code=404, detail="Academic year not found")
    values = payload.model_dump(exclude_unset=True)
    start_date = values.get("start_date", year.start_date)
    end_date = values.get("end_date", year.end_date)
    _valid_dates(start_date, end_date)
    if values.get("is_current"):
        current = await session.execute(select(AcademicYear).where(AcademicYear.school_id == school_id, AcademicYear.id != year_id, AcademicYear.is_current == True))
        closing_year_ids = []
        for other in current.scalars().all():
            other.is_current = False
            if other.status == AcademicYearStatus.ACTIVE:
                other.status = AcademicYearStatus.CLOSED
            closing_year_ids.append(other.id)
        await _lock_terms_for_years(session, school_id, closing_year_ids)
        values["status"] = AcademicYearStatus.ACTIVE
    for key, value in values.items():
        setattr(year, key, value)
    year.updated_at = datetime.utcnow()
    session.add(year)
    await session.commit()
    await session.refresh(year)
    return _year_dict(year)


@router.get("/events", response_model=list[dict])
async def list_calendar_events(
    academic_year_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_scope(current_user)
    query = select(CalendarEvent).where(CalendarEvent.school_id == school_id)
    if academic_year_id:
        query = query.where(CalendarEvent.academic_year_id == academic_year_id)
    result = await session.execute(query.order_by(CalendarEvent.start_date, CalendarEvent.title))
    return [_event_dict(event) for event in result.scalars().all()]


@router.post("/events", response_model=dict)
async def create_calendar_event(
    payload: CalendarEventCreate,
    current_user: User = Depends(require_permission("academics.calendar_event.manage")),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_scope(current_user)
    _valid_dates(payload.start_date, payload.end_date)
    if payload.academic_year_id:
        year = await session.execute(select(AcademicYear).where(AcademicYear.id == payload.academic_year_id, AcademicYear.school_id == school_id))
        if not year.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Academic year does not belong to this school")
    event = CalendarEvent(school_id=school_id, created_by=current_user.id, **payload.model_dump())
    session.add(event)
    await session.commit()
    await session.refresh(event)
    return _event_dict(event)


@router.put("/events/{event_id}", response_model=dict)
async def update_calendar_event(
    event_id: str,
    payload: CalendarEventUpdate,
    current_user: User = Depends(require_permission("academics.calendar_event.manage")),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_scope(current_user)
    result = await session.execute(select(CalendarEvent).where(CalendarEvent.id == event_id, CalendarEvent.school_id == school_id))
    event = result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Calendar event not found")
    values = payload.model_dump(exclude_unset=True)
    _valid_dates(values.get("start_date", event.start_date), values.get("end_date", event.end_date))
    for key, value in values.items():
        setattr(event, key, value)
    event.updated_at = datetime.utcnow()
    session.add(event)
    await session.commit()
    await session.refresh(event)
    return _event_dict(event)


@router.delete("/events/{event_id}", response_model=dict)
async def delete_calendar_event(
    event_id: str,
    current_user: User = Depends(require_permission("academics.calendar_event.manage")),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_scope(current_user)
    result = await session.execute(select(CalendarEvent).where(CalendarEvent.id == event_id, CalendarEvent.school_id == school_id))
    event = result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Calendar event not found")
    await session.delete(event)
    await session.commit()
    return {"message": "Calendar event deleted"}


# ==================== Calendar sync: .ics export + subscribable feed ====
# One-time download (auth'd, JWT) vs. a subscribable feed (public, keyed by
# a per-school secret token — Google Calendar/Outlook's "subscribe from
# URL" flows poll on their own schedule with no session available).

def _feed_url(request: Request, token: str) -> str:
    return f"{str(request.base_url).rstrip('/')}/api/academic-calendar/feed/{token}.ics"


@router.get("/export.ics")
async def export_calendar_ics(
    academic_year_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_scope(current_user)
    query = select(CalendarEvent).where(CalendarEvent.school_id == school_id)
    if academic_year_id:
        query = query.where(CalendarEvent.academic_year_id == academic_year_id)
    result = await session.execute(query.order_by(CalendarEvent.start_date))
    ics_content = calendar_ics_service.build_ics(result.scalars().all(), calendar_name="Academic Calendar")
    return Response(
        content=ics_content, media_type=CALENDAR_MEDIA_TYPE,
        headers={"Content-Disposition": "attachment; filename=academic-calendar.ics"},
    )


@router.get("/feed-token", response_model=dict)
async def get_calendar_feed_token(
    request: Request,
    current_user: User = Depends(require_permission("academics.calendar_feed_token.manage")),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_scope(current_user)
    result = await session.execute(select(CalendarFeedToken).where(CalendarFeedToken.school_id == school_id))
    token_row = result.scalar_one_or_none()
    if not token_row:
        return {"enabled": False, "feed_url": None, "created_at": None, "rotated_at": None}
    return {
        "enabled": True, "feed_url": _feed_url(request, token_row.token),
        "created_at": token_row.created_at, "rotated_at": token_row.rotated_at,
    }


@router.post("/feed-token/regenerate", response_model=dict)
async def regenerate_calendar_feed_token(
    request: Request,
    current_user: User = Depends(require_permission("academics.calendar_feed_token.manage")),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session),
):
    """Creates the feed on first call, rotates (invalidating the previous
    URL) on every call after that."""
    school_id = _school_scope(current_user)
    result = await session.execute(select(CalendarFeedToken).where(CalendarFeedToken.school_id == school_id))
    token_row = result.scalar_one_or_none()
    new_token = calendar_ics_service.generate_feed_token()
    if token_row:
        token_row.token = new_token
        token_row.rotated_at = datetime.utcnow()
    else:
        token_row = CalendarFeedToken(school_id=school_id, token=new_token, created_by=current_user.id)
    session.add(token_row)
    await session.commit()
    await session.refresh(token_row)
    return {"enabled": True, "feed_url": _feed_url(request, token_row.token)}


@router.delete("/feed-token", response_model=dict)
async def revoke_calendar_feed_token(
    current_user: User = Depends(require_permission("academics.calendar_feed_token.manage")),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_scope(current_user)
    result = await session.execute(select(CalendarFeedToken).where(CalendarFeedToken.school_id == school_id))
    token_row = result.scalar_one_or_none()
    if token_row:
        await session.delete(token_row)
        await session.commit()
    return {"enabled": False, "feed_url": None}


@router.get("/feed/{token}.ics")
async def calendar_feed(
    token: str,
    session: AsyncSession = Depends(get_session),
):
    """Public — no auth dependency. The token in the path is the only
    authentication, since Google Calendar/Outlook fetch this on a recurring
    schedule with no Campusio session available."""
    result = await session.execute(select(CalendarFeedToken).where(CalendarFeedToken.token == token))
    token_row = result.scalar_one_or_none()
    if not token_row:
        raise HTTPException(status_code=404, detail="Invalid or revoked calendar feed link")
    events_result = await session.execute(
        select(CalendarEvent).where(CalendarEvent.school_id == token_row.school_id).order_by(CalendarEvent.start_date)
    )
    ics_content = calendar_ics_service.build_ics(events_result.scalars().all(), calendar_name="Academic Calendar")
    return Response(content=ics_content, media_type=CALENDAR_MEDIA_TYPE)


async def _promotion_decision_gaps(session: AsyncSession, source_term_id: str, student_ids: list[str]) -> tuple[int, list[str]]:
    """Among student_ids, which have NO explicit ReportCard.promotion_decision
    recorded for source_term_id. apply_rollover defaults a student with no
    explicit decision to PROMOTED — silently, since "promoted" is the normal
    case for most students most years. That default is reasonable, but it
    means a student who should have repeated a grade gets carried forward
    anyway if nobody got around to marking their report card. Surfaced here
    so an admin can review/correct report cards before or after rollover,
    rather than only discovering it once the student is already in the
    wrong class."""
    if not student_ids:
        return 0, []
    cards = await session.execute(
        select(ReportCard.student_id).where(
            ReportCard.academic_term_id == source_term_id,
            ReportCard.student_id.in_(student_ids),
            ReportCard.promotion_decision.is_not(None),
        )
    )
    decided_ids = {row[0] for row in cards.all()}
    missing = [sid for sid in student_ids if sid not in decided_ids]
    return len(missing), missing


async def _rollover_counts(school_id: str, session: AsyncSession) -> dict:
    current_term_result = await session.execute(select(AcademicTerm).where(AcademicTerm.school_id == school_id, AcademicTerm.is_current == True))
    current_term = current_term_result.scalar_one_or_none()
    if not current_term:
        raise HTTPException(status_code=400, detail="Set a current academic term before starting rollover")
    classes_result = await session.execute(select(Class).where(Class.school_id == school_id, Class.is_active == True))
    students_result = await session.execute(select(Student).where(Student.school_id == school_id, Student.status == StudentStatus.ACTIVE, Student.class_id.is_not(None)))
    students = students_result.scalars().all()
    gap_count, gap_student_ids = await _promotion_decision_gaps(session, current_term.id, [s.id for s in students])
    return {
        "source_term_id": current_term.id,
        "source_academic_year": current_term.academic_year,
        "class_count": len(classes_result.scalars().all()),
        "active_student_count": len(students),
        "students_without_promotion_decision": gap_count,
        "students_without_promotion_decision_ids": gap_student_ids,
    }


@router.post("/rollover/preview", response_model=dict)
async def preview_rollover(
    payload: RolloverRequest,
    current_user: User = Depends(require_permission("academics.calendar_rollover.manage")),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_scope(current_user)
    _valid_dates(payload.start_date, payload.end_date)
    if len(payload.terms) != 3 or {item.term for item in payload.terms} != set(TermType):
        raise HTTPException(status_code=400, detail="Rollover requires first, second, and third term dates")
    for item in payload.terms:
        _valid_dates(item.start_date, item.end_date)
    counts = await _rollover_counts(school_id, session)
    return {"ready": True, "target_academic_year": payload.name, "terms": [item.model_dump() for item in payload.terms], **counts}


@router.post("/rollover", response_model=dict)
async def apply_rollover(
    payload: RolloverRequest,
    current_user: User = Depends(require_permission("academics.calendar_rollover.manage")),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_scope(current_user)
    _valid_dates(payload.start_date, payload.end_date)
    if len(payload.terms) != 3 or {item.term for item in payload.terms} != set(TermType):
        raise HTTPException(status_code=400, detail="Rollover requires first, second, and third term dates")
    existing = await session.execute(select(AcademicYear).where(AcademicYear.school_id == school_id, AcademicYear.name == payload.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="This academic year already exists; rollover is not run twice")
    counts = await _rollover_counts(school_id, session)
    source_term_id = counts["source_term_id"]

    target_year = AcademicYear(school_id=school_id, name=payload.name, start_date=payload.start_date, end_date=payload.end_date, status=AcademicYearStatus.ACTIVE, is_current=True)
    session.add(target_year)

    # Close the previous year(s) and lock every term under them — this is
    # the historical-locking side of rollover: once a new year starts, the
    # old one's grades/attendance/fees/assignments stop accepting writes.
    current_years = await session.execute(select(AcademicYear).where(AcademicYear.school_id == school_id, AcademicYear.id != target_year.id, AcademicYear.is_current == True))
    closing_year_ids = []
    for year in current_years.scalars().all():
        year.is_current = False
        year.status = AcademicYearStatus.CLOSED
        closing_year_ids.append(year.id)
    await _lock_terms_for_years(session, school_id, closing_year_ids)
    # The outgoing current term might not be linked to an AcademicYear at all
    # (e.g. it was created through the older free-text Settings flow before
    # this year/rollover system existed) — lock it directly too, regardless
    # of that linkage, so "the term rollover just moved past" is always locked.
    source_term_row = await session.execute(select(AcademicTerm).where(AcademicTerm.id == source_term_id))
    source_term = source_term_row.scalar_one_or_none()
    if source_term:
        source_term.is_locked = True
        source_term.is_current = False

    await session.flush()
    term_by_type = {}
    for item in payload.terms:
        _valid_dates(item.start_date, item.end_date)
        term = AcademicTerm(school_id=school_id, academic_year_id=target_year.id, academic_year=payload.name, term=item.term, start_date=item.start_date, end_date=item.end_date, is_current=item.term == TermType.FIRST)
        session.add(term)
        term_by_type[item.term] = term
    await session.flush()
    target_first_term = term_by_type[TermType.FIRST]

    class_map: dict[str, str] = {}
    level_by_source_class: dict[str, ClassLevel] = {}
    section_by_source_class: dict[str, Optional[str]] = {}
    clone_by_level_section: dict[tuple, str] = {}
    clone_by_level: dict[ClassLevel, list[str]] = {}
    cloned_classes = 0
    cloned_timetable_entries = 0
    if payload.clone_classes:
        source_classes = await session.execute(select(Class).where(Class.school_id == school_id, Class.is_active == True))
        for source in source_classes.scalars().all():
            clone = Class(school_id=school_id, name=source.name, level=source.level, section=source.section, capacity=source.capacity, room_number=source.room_number, academic_term_id=target_first_term.id, campus_id=source.campus_id, is_active=True)
            session.add(clone)
            await session.flush()
            class_map[source.id] = clone.id
            level_by_source_class[source.id] = source.level
            section_by_source_class[source.id] = source.section
            clone_by_level_section[(source.level, source.section)] = clone.id
            clone_by_level.setdefault(source.level, []).append(clone.id)
            cloned_classes += 1
            links = await session.execute(select(ClassSubject).where(ClassSubject.school_id == school_id, ClassSubject.class_id == source.id, ClassSubject.academic_term_id == source_term_id))
            for link in links.scalars().all():
                session.add(ClassSubject(id=str(uuid.uuid4()), school_id=school_id, class_id=clone.id, subject_id=link.subject_id, academic_term_id=target_first_term.id))
            assignments = await session.execute(select(TeacherAssignment).where(TeacherAssignment.school_id == school_id, TeacherAssignment.class_id == source.id, TeacherAssignment.academic_term_id == source_term_id))
            for assignment in assignments.scalars().all():
                session.add(TeacherAssignment(id=str(uuid.uuid4()), school_id=school_id, staff_id=assignment.staff_id, class_id=clone.id, subject_id=assignment.subject_id, academic_term_id=target_first_term.id, is_class_teacher=assignment.is_class_teacher))
            if payload.clone_timetable:
                slots = await session.execute(select(Timetable).where(Timetable.school_id == school_id, Timetable.class_id == source.id, Timetable.academic_term_id == source_term_id))
                for slot in slots.scalars().all():
                    session.add(Timetable(school_id=school_id, class_id=clone.id, subject_id=slot.subject_id, teacher_id=slot.teacher_id, period_id=slot.period_id, day_of_week=slot.day_of_week, academic_term_id=target_first_term.id, room=slot.room))
                    cloned_timetable_entries += 1
    elif payload.carry_students:
        # clone_classes=False but carry_students=True: Class.academic_term_id
        # is optional (models/classroom.py) precisely because a school's
        # classes are commonly evergreen -- the same Class rows ("JHS 2A")
        # get reused year over year rather than recreated -- so "clone
        # classes for the new year" is correctly left unchecked when that's
        # how a school runs. class_map/clone_by_level(_section) previously
        # stayed empty in this combination regardless (only ever populated
        # inside the clone_classes branch above), so the promotion loop
        # below -- gated on `if payload.carry_students and class_map` --
        # silently never ran: no students promoted, no error, no warning,
        # just a response reporting zero of everything. There's no separate
        # "new year" class set to clone into here, so resolve the same
        # lookup structures directly from the currently-active classes
        # themselves, matching by (level, section) the same way the
        # promoted-student branch below already does for a missing exact
        # match.
        source_classes_result = await session.execute(select(Class).where(Class.school_id == school_id, Class.is_active == True))
        for source in source_classes_result.scalars().all():
            level_by_source_class[source.id] = source.level
            section_by_source_class[source.id] = source.section
            clone_by_level_section[(source.level, source.section)] = source.id
            clone_by_level.setdefault(source.level, []).append(source.id)
            # A repeating student has nowhere else to go without a fresh
            # clone -- they stay in the same (evergreen) class for another year.
            class_map[source.id] = source.id

    cloned_fee_structures = 0
    if payload.clone_fee_structures:
        structures = await session.execute(select(FeeStructure).where(FeeStructure.school_id == school_id, FeeStructure.academic_term_id == source_term_id))
        for structure in structures.scalars().all():
            # Template only — no per-student Fee rows are created here, so no
            # money moves without a human explicitly assigning the structure
            # to a class (POST /fees/assign-class), same as any other term.
            session.add(FeeStructure(school_id=school_id, academic_term_id=target_first_term.id, class_level=structure.class_level, campus_id=structure.campus_id, fee_type=structure.fee_type, amount=structure.amount, description=structure.description, is_mandatory=structure.is_mandatory, due_date=structure.due_date))
            cloned_fee_structures += 1

    students_promoted = 0
    students_repeated = 0
    students_graduated = 0
    students_unmatched = 0
    students_defaulted = 0
    students_waitlisted = 0
    # level_by_source_class (not class_map) is the "every active source class
    # considered" set in both branches above -- class_map itself can be
    # legitimately incomplete when carry_students runs without clone_classes
    # (only classes with a matching existing target get an entry), and
    # filtering the student query on class_map.keys() would silently drop
    # students whose class never matched, the same silent-no-op shape this
    # whole fix targets, just at per-class instead of whole-operation
    # granularity.
    if payload.carry_students and level_by_source_class:
        students_result = await session.execute(select(Student).where(Student.school_id == school_id, Student.status == StudentStatus.ACTIVE, Student.class_id.in_(list(level_by_source_class.keys()))))
        students = students_result.scalars().all()
        student_ids = [s.id for s in students]
        decision_by_student: dict[str, str] = {}
        if student_ids:
            cards = await session.execute(select(ReportCard).where(ReportCard.academic_term_id == source_term_id, ReportCard.student_id.in_(student_ids)))
            for card in cards.scalars().all():
                if card.promotion_decision:
                    decision_by_student[card.student_id] = card.promotion_decision

        for student in students:
            source_level = level_by_source_class[student.class_id]
            source_section = section_by_source_class[student.class_id]
            has_explicit_decision = student.id in decision_by_student
            decision = decision_by_student.get(student.id, PromotionDecision.PROMOTED.value)

            if decision == PromotionDecision.GRADUATED.value or (
                decision != PromotionDecision.REPEATED.value and LEVEL_INDEX[source_level] == len(LEVEL_ORDER) - 1
            ):
                # Explicitly marked graduated, or promoted out of the last level (JHS_3).
                student.status = StudentStatus.GRADUATED
                student.class_id = None
                open_rows = await session.execute(select(StudentEnrollment).where(StudentEnrollment.student_id == student.id, StudentEnrollment.ended_at.is_(None)))
                for row in open_rows.scalars().all():
                    row.ended_at = datetime.utcnow()
                    row.ended_reason = "graduated"
                students_graduated += 1
                if not has_explicit_decision:
                    # No ReportCard.promotion_decision was ever recorded —
                    # graduated on the "promoted out of the last level"
                    # default, not a human's actual decision. Counted so
                    # it's visible in the response rather than only
                    # discoverable after the fact.
                    students_defaulted += 1
                continue

            if decision == PromotionDecision.REPEATED.value:
                # .get(), not direct indexing -- class_map can legitimately
                # have no entry for this source class when carry_students
                # runs without clone_classes (only classes with a matching
                # existing target get mapped), so a missing entry must fall
                # through to the same "unmatched" handling below rather than
                # raising a KeyError.
                target_class_id = class_map.get(student.class_id)
            else:
                next_level = LEVEL_ORDER[LEVEL_INDEX[source_level] + 1]
                target_class_id = clone_by_level_section.get((next_level, source_section))
                if not target_class_id:
                    # No class at the next level shares this student's section --
                    # previously silently placed the student into whichever
                    # class happened to be first at that level (dict iteration
                    # order), with no record anything was uncertain. Waitlist
                    # against the first available class at that level instead,
                    # for manual review -- the same "don't guess, queue for a
                    # human" choice this function already makes below for
                    # over-capacity students.
                    fallback_class_id = next(iter(clone_by_level.get(next_level, [])), None)
                    if fallback_class_id:
                        await _add_to_waitlist(session, school_id, fallback_class_id, student.id)
                        students_waitlisted += 1
                        open_rows = await session.execute(select(StudentEnrollment).where(StudentEnrollment.student_id == student.id, StudentEnrollment.ended_at.is_(None)))
                        for row in open_rows.scalars().all():
                            row.ended_at = datetime.utcnow()
                            row.ended_reason = "rolled_over_waitlisted"
                        student.class_id = None
                        continue

            if not target_class_id:
                students_unmatched += 1
                open_rows = await session.execute(select(StudentEnrollment).where(StudentEnrollment.student_id == student.id, StudentEnrollment.ended_at.is_(None)))
                for row in open_rows.scalars().all():
                    row.ended_at = datetime.utcnow()
                    row.ended_reason = "rolled_over_unmatched"
                student.class_id = None
                continue

            # A cloned class starts with the same capacity as its source —
            # normal for a same-size cohort, but a level with more students
            # than seats (repeats piling on top of promotions, or an uneven
            # class-size year) could otherwise silently push a class over
            # capacity with no record of it. Same check_class_capacity used
            # everywhere else a student's class_id is set (create_student,
            # convert_applicant, ...) — auto-waitlists instead of blocking
            # the whole rollover over one student.
            waitlist_entry = await check_class_capacity(session, school_id, target_class_id, auto_waitlist=True, student_id=student.id)
            if waitlist_entry is not None:
                students_waitlisted += 1
                open_rows = await session.execute(select(StudentEnrollment).where(StudentEnrollment.student_id == student.id, StudentEnrollment.ended_at.is_(None)))
                for row in open_rows.scalars().all():
                    row.ended_at = datetime.utcnow()
                    row.ended_reason = "rolled_over_waitlisted"
                student.class_id = None
                continue

            if decision == PromotionDecision.REPEATED.value:
                students_repeated += 1
            else:
                students_promoted += 1
                if not has_explicit_decision:
                    # Same default-promoted case as the graduated branch
                    # above, for the normal (non-last-level) path.
                    students_defaulted += 1

            open_rows = await session.execute(select(StudentEnrollment).where(StudentEnrollment.student_id == student.id, StudentEnrollment.ended_at.is_(None)))
            for row in open_rows.scalars().all():
                row.ended_at = datetime.utcnow()
                row.ended_reason = "rolled_over"
            student.class_id = target_class_id
            session.add(StudentEnrollment(school_id=school_id, student_id=student.id, class_id=target_class_id, academic_term_id=target_first_term.id, ended_reason=None))

    await session.commit()
    return {
        "message": "Academic year rollover completed",
        "academic_year": _year_dict(target_year),
        "terms_created": 3,
        "classes_cloned": cloned_classes,
        "timetable_entries_cloned": cloned_timetable_entries,
        "fee_structures_cloned": cloned_fee_structures,
        "students_carried": students_promoted + students_repeated + students_graduated,
        "students_promoted": students_promoted,
        "students_repeated": students_repeated,
        "students_graduated": students_graduated,
        "students_unmatched": students_unmatched,
        # Their target class (repeat or promote) was already at capacity —
        # waitlisted (ClassWaitlistEntry, same as any other over-capacity
        # assignment) rather than silently pushing the cloned class over
        # its seat count. Not counted in students_promoted/students_repeated.
        "students_waitlisted": students_waitlisted,
        # Of the students promoted/graduated above, how many had no explicit
        # ReportCard.promotion_decision and were carried on the default —
        # review these to make sure nobody who should have repeated a grade
        # was promoted just because their report card was never marked.
        "students_defaulted_without_decision": students_defaulted,
        "source_term_id": source_term_id,
    }