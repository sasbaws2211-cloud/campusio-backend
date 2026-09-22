"""Elective / subject-track management router — school-wide track catalog,
per-track subject assignment, and per-student track enrollment. See
models/tracks.py for the join-table shapes this operates on."""
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user, require_permission, require_roles
from database import get_session
from models.tracks import (
    Track, TrackCreate, TrackUpdate,
    TrackSubject, TrackSubjectCreate,
    StudentTrack, StudentTrackCreate,
)
from models.classroom import Class, Subject
from models.student import Student
from models.school import AcademicTerm
from models.user import User, UserRole

router = APIRouter(tags=["Tracks"])

# Track rosters and per-student track membership are not public directory
# info -- previously every read endpoint below was open to any same-school
# user via plain get_current_user, including STUDENT/PARENT accounts (e.g.
# any logged-in student could pull a full track roster by id). Restricted
# to staff.
STAFF_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.TEACHER)


def school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


async def validate_track_level(session: AsyncSession, student: Student, track: Track) -> None:
    """A track with class_level set is only enrollable by a student whose
    current class is at that level — e.g. a "Science Electives" track meant
    for JHS students shouldn't silently accept a Primary 1 student just
    because nothing checked. A track with class_level=None (the default) is
    open to every level, so this is a no-op for the common case.

    Fails closed: a student with no class_id can't be matched against a
    level-scoped track, so that's rejected too rather than silently allowed
    through — the same "can't verify it, so don't allow it" stance used
    elsewhere in this codebase (e.g. exam-paper approval gating)."""
    if not track.class_level:
        return
    if not student.class_id:
        raise HTTPException(
            status_code=400,
            detail=f"Student is not currently assigned to a class, so eligibility for this {track.class_level}-only track can't be verified",
        )
    class_result = await session.execute(select(Class).where(Class.id == student.class_id))
    student_class = class_result.scalar_one_or_none()
    if not student_class or student_class.level.value != track.class_level:
        raise HTTPException(
            status_code=400,
            detail=f"This track is only open to {track.class_level} students",
        )


@router.get("/tracks", response_model=list[dict])
async def list_tracks(
    academic_term_id: str | None = None,
    is_active: bool | None = None,
    user: User = Depends(require_roles(*STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    scope = school_id(user)
    query = select(Track).where(Track.school_id == scope)
    if academic_term_id:
        query = query.where(Track.academic_term_id == academic_term_id)
    if is_active is not None:
        query = query.where(Track.is_active == is_active)
    result = await session.execute(query.order_by(Track.name))
    tracks = result.scalars().all()

    track_ids = [t.id for t in tracks]
    subject_counts: dict = {}
    student_counts: dict = {}
    if track_ids:
        subj_result = await session.execute(
            select(TrackSubject.track_id, func.count(TrackSubject.id)).where(TrackSubject.track_id.in_(track_ids)).group_by(TrackSubject.track_id)
        )
        subject_counts = dict(subj_result.all())
        student_result = await session.execute(
            select(StudentTrack.track_id, func.count(StudentTrack.id)).where(StudentTrack.track_id.in_(track_ids)).group_by(StudentTrack.track_id)
        )
        student_counts = dict(student_result.all())

    return [
        {**t.model_dump(), "subject_count": subject_counts.get(t.id, 0), "student_count": student_counts.get(t.id, 0)}
        for t in tracks
    ]


@router.post("/tracks", response_model=dict)
async def create_track(payload: TrackCreate, user: User = Depends(require_permission("academics.track.manage")), session: AsyncSession = Depends(get_session)):
    # academic_term_id had no existence check -- unlike enroll_student_in_track
    # (below), which already validates it, letting a track end up with a
    # dangling or cross-tenant academic_term_id.
    if payload.academic_term_id:
        term_result = await session.execute(select(AcademicTerm).where(AcademicTerm.id == payload.academic_term_id, AcademicTerm.school_id == school_id(user)))
        if not term_result.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="academic_term_id does not exist for this school")
    item = Track(school_id=school_id(user), **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.patch("/tracks/{track_id}", response_model=dict)
async def update_track(track_id: str, payload: TrackUpdate, user: User = Depends(require_permission("academics.track.manage")), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Track).where(Track.id == track_id, Track.school_id == school_id(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Track not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.get("/tracks/{track_id}/subjects", response_model=list[dict])
async def list_track_subjects(track_id: str, user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    scope = school_id(user)
    track_result = await session.execute(select(Track).where(Track.id == track_id, Track.school_id == scope))
    if not track_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Track not found")
    result = await session.execute(select(TrackSubject).where(TrackSubject.track_id == track_id, TrackSubject.school_id == scope))
    return [item.model_dump() for item in result.scalars().all()]


@router.post("/tracks/{track_id}/subjects", response_model=dict)
async def add_track_subject(
    track_id: str, payload: TrackSubjectCreate,
    user: User = Depends(require_permission("academics.track_subject.manage")), session: AsyncSession = Depends(get_session),
):
    scope = school_id(user)
    track_result = await session.execute(select(Track).where(Track.id == track_id, Track.school_id == scope))
    if not track_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Track not found")

    # subject_id had no existence check -- unlike routers/classes.py's
    # assign_subject_to_class, which validates its own Subject the same way.
    subject_result = await session.execute(select(Subject).where(Subject.id == payload.subject_id, Subject.school_id == scope))
    if not subject_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="subject_id does not exist for this school")

    existing = await session.execute(select(TrackSubject).where(
        TrackSubject.track_id == track_id, TrackSubject.subject_id == payload.subject_id, TrackSubject.school_id == scope
    ))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Subject is already assigned to this track")

    item = TrackSubject(school_id=scope, track_id=track_id, subject_id=payload.subject_id)
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.delete("/tracks/{track_id}/subjects/{subject_id}", response_model=dict)
async def remove_track_subject(
    track_id: str, subject_id: str,
    user: User = Depends(require_permission("academics.track_subject.manage")), session: AsyncSession = Depends(get_session),
):
    scope = school_id(user)
    result = await session.execute(select(TrackSubject).where(
        TrackSubject.track_id == track_id, TrackSubject.subject_id == subject_id, TrackSubject.school_id == scope
    ))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Subject is not assigned to this track")
    await session.delete(item)
    await session.commit()
    return {"message": "Subject removed from track"}


@router.get("/tracks/{track_id}/students", response_model=list[dict])
async def list_track_students(track_id: str, user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    scope = school_id(user)
    track_result = await session.execute(select(Track).where(Track.id == track_id, Track.school_id == scope))
    if not track_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Track not found")
    result = await session.execute(select(StudentTrack).where(StudentTrack.track_id == track_id, StudentTrack.school_id == scope))
    entries = result.scalars().all()

    student_ids = [e.student_id for e in entries]
    student_names: dict = {}
    if student_ids:
        student_result = await session.execute(select(Student).where(Student.id.in_(student_ids)))
        student_names = {s.id: f"{s.first_name} {s.last_name}" for s in student_result.scalars().all()}

    return [{**e.model_dump(), "student_name": student_names.get(e.student_id, "Unknown")} for e in entries]


@router.post("/tracks/{track_id}/students", response_model=dict)
async def enroll_student_in_track(
    track_id: str, payload: StudentTrackCreate,
    user: User = Depends(require_permission("academics.track_student.manage")), session: AsyncSession = Depends(get_session),
):
    scope = school_id(user)
    track_result = await session.execute(select(Track).where(Track.id == track_id, Track.school_id == scope))
    track = track_result.scalar_one_or_none()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    student_result = await session.execute(select(Student).where(Student.id == payload.student_id, Student.school_id == scope))
    student = student_result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=400, detail="student_id does not exist for this school")

    await validate_track_level(session, student, track)

    term_result = await session.execute(select(AcademicTerm).where(AcademicTerm.id == payload.academic_term_id, AcademicTerm.school_id == scope))
    if not term_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="academic_term_id does not exist for this school")

    # A student can only be on one track per term — check across ALL tracks,
    # not just this one, before creating the enrollment.
    existing = await session.execute(select(StudentTrack).where(
        StudentTrack.student_id == payload.student_id,
        StudentTrack.academic_term_id == payload.academic_term_id,
        StudentTrack.school_id == scope,
    ))
    existing_row = existing.scalar_one_or_none()
    if existing_row:
        if existing_row.track_id == track_id:
            raise HTTPException(status_code=400, detail="Student is already enrolled in this track for this term")
        raise HTTPException(status_code=400, detail="Student is already enrolled in a different track for this term — remove that enrollment first")

    item = StudentTrack(school_id=scope, track_id=track_id, student_id=payload.student_id, academic_term_id=payload.academic_term_id)
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.delete("/student-tracks/{id}", response_model=dict)
async def remove_student_track(id: str, user: User = Depends(require_permission("academics.track_student.manage")), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(StudentTrack).where(StudentTrack.id == id, StudentTrack.school_id == school_id(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Student track enrollment not found")
    await session.delete(item)
    await session.commit()
    return {"message": "Removed student from track"}


@router.get("/students/{student_id}/track", response_model=dict)
async def get_student_current_track(student_id: str, user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    """The student's track for the school's current academic term, if any."""
    scope = school_id(user)
    student_result = await session.execute(select(Student).where(Student.id == student_id, Student.school_id == scope))
    if not student_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Student not found")

    term_result = await session.execute(select(AcademicTerm).where(AcademicTerm.school_id == scope, AcademicTerm.is_current == True))  # noqa: E712
    term = term_result.scalar_one_or_none()
    if not term:
        return {"track": None, "student_track_id": None, "academic_term_id": None}

    st_result = await session.execute(select(StudentTrack).where(
        StudentTrack.student_id == student_id, StudentTrack.academic_term_id == term.id, StudentTrack.school_id == scope
    ))
    student_track = st_result.scalar_one_or_none()
    if not student_track:
        return {"track": None, "student_track_id": None, "academic_term_id": term.id}

    track_result = await session.execute(select(Track).where(Track.id == student_track.track_id))
    track = track_result.scalar_one_or_none()
    return {
        "track": track.model_dump() if track else None,
        "student_track_id": student_track.id,
        "academic_term_id": term.id,
    }
