"""Curriculum framework, schemes of work, lesson plans, and coverage."""
from datetime import datetime, date, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user, require_permission, require_roles
from database import get_session
from models.curriculum import (
    CurriculumTopic, CurriculumTopicCreate, CurriculumTopicUpdate, LessonPlan, LessonPlanCreate, LessonPlanUpdate,
    TeacherLessonNote, TeacherLessonNoteCreate, TeacherLessonNoteUpdate, TopicCoverageUpdate,
    CurriculumStandard, CurriculumStandardCreate, CurriculumStandardUpdate,
    TopicStandardLink, TopicStandardLinkCreate,
)
from models.user import User, UserRole
from models.staff import Staff, TeacherAssignment
from models.classroom import Class, Subject
from models.school import AcademicTerm
from dependencies import assert_campus_access

router = APIRouter(prefix="/curriculum", tags=["Curriculum"])


def school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


ADMIN_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)
# Lesson notes/plans/topics/standards/coverage are teacher-internal working
# content, not public student/parent directory info -- previously every
# list endpoint below was open to any same-school user via plain
# get_current_user, including STUDENT/PARENT accounts. Restricted to staff.
STAFF_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.TEACHER)


async def _own_staff_id(user: User, session: AsyncSession) -> str | None:
    """The caller's own linked Staff record, if any — None rather than
    raising, so admin callers without a teaching profile can fall through
    to the teacher_id override instead of hitting a hard error."""
    result = await session.execute(select(Staff).where(Staff.school_id == school_id(user), Staff.user_id == user.id))
    staff = result.scalar_one_or_none()
    if not staff:
        result = await session.execute(select(Staff).where(Staff.school_id == school_id(user), Staff.email == user.email))
        staff = result.scalar_one_or_none()
    return staff.id if staff else None


async def teacher_staff_id(user: User, session: AsyncSession) -> str:
    staff_id = await _own_staff_id(user, session)
    if not staff_id:
        raise HTTPException(status_code=404, detail="Teacher staff profile not found")
    return staff_id


async def assert_teaches_class_subject(user: User, session: AsyncSession, teacher_staff_id_: str, class_id: str, subject_id: str) -> None:
    """A TEACHER can only record lesson notes/plans/coverage for a class+
    subject they're actually assigned to teach — the same TeacherAssignment
    check routers/teacher/grades.py already enforces for grade recording.
    Curriculum has no separate teacher-portal router the way grades does, so
    without this a teacher's broad academics.lesson_note.manage/
    academics.lesson_plan.manage/academics.topic.manage grant (school-wide,
    not per-assignment — see scripts/seed_permissions.py) would let them
    write lesson content for ANY class/subject in the school, not just their
    own. No-op for admins, who legitimately manage curriculum school-wide."""
    if user.role != UserRole.TEACHER:
        return
    result = await session.execute(select(TeacherAssignment).where(
        TeacherAssignment.school_id == school_id(user),
        TeacherAssignment.staff_id == teacher_staff_id_,
        TeacherAssignment.class_id == class_id,
        TeacherAssignment.subject_id == subject_id,
    ))
    # A teacher can be assigned this class+subject across multiple academic
    # terms, so more than one row can match here — only need to know at
    # least one exists.
    if not result.scalars().first():
        raise HTTPException(status_code=403, detail="You are not assigned to teach this class/subject")


async def assert_class_campus_access(session: AsyncSession, user: User, class_id: str) -> None:
    """curriculum.py previously had zero campus scoping anywhere -- a
    campus-scoped SCHOOL_ADMIN/TEACHER could write lesson notes/plans/topics
    for a class in a DIFFERENT campus of the same school. Existence
    validation of a bogus/foreign class_id is now handled here too (was a
    separate, deferred Tier-2 finding at the time this was first written)."""
    result = await session.execute(select(Class).where(Class.id == class_id, Class.school_id == school_id(user)))
    cls = result.scalar_one_or_none()
    if not cls:
        raise HTTPException(status_code=400, detail="class_id does not exist for this school")
    assert_campus_access(user, cls.campus_id)


async def _validate_curriculum_references(session: AsyncSession, school_id_: str, subject_id: str, academic_term_id: str | None = None) -> None:
    """curriculum.py's create endpoints previously accepted subject_id/
    academic_term_id with no existence check for admin-authored content (a
    TEACHER caller gets incidental protection via assert_teaches_class_subject's
    TeacherAssignment lookup, but that's a no-op for ADMIN/SUPER_ADMIN) -- an
    admin could create a CurriculumTopic/TeacherLessonNote/LessonPlan row
    referencing a subject_id/academic_term_id that doesn't exist at all, or
    belongs to a different tenant. Mirrors
    routers/classes.py::validate_academic_term and
    routers/timetable.py::validate_timetable_references. class_id's own
    existence is already checked by assert_class_campus_access above."""
    subject_result = await session.execute(select(Subject).where(Subject.id == subject_id, Subject.school_id == school_id_))
    if not subject_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="subject_id does not exist for this school")
    if academic_term_id:
        term_result = await session.execute(select(AcademicTerm).where(AcademicTerm.id == academic_term_id, AcademicTerm.school_id == school_id_))
        if not term_result.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="academic_term_id does not exist for this school")


async def resolve_teacher_id(user: User, session: AsyncSession, override_teacher_id: str | None) -> str:
    """Whose lesson content this is. Teachers can only ever author as
    themselves — override_teacher_id is ignored for them, so one teacher
    can't attribute a plan/note to another. Admins/super-admins may author
    on behalf of any teacher at the school via override_teacher_id; if they
    don't provide one, fall back to their own staff profile (some admins
    are also teaching staff) before giving up with a clear 400."""
    if user.role == UserRole.TEACHER:
        return await teacher_staff_id(user, session)
    if user.role in ADMIN_ROLES:
        if override_teacher_id:
            staff = (await session.execute(select(Staff).where(Staff.id == override_teacher_id, Staff.school_id == school_id(user)))).scalar_one_or_none()
            if not staff:
                raise HTTPException(status_code=400, detail="teacher_id does not exist for this school")
            return staff.id
        own = await _own_staff_id(user, session)
        if own:
            return own
        raise HTTPException(status_code=400, detail="Select which teacher this content is for (teacher_id) — your account has no linked staff profile")
    return await teacher_staff_id(user, session)


@router.get("/lesson-notes", response_model=list[dict])
async def list_lesson_notes(class_id: str | None = None, subject_id: str | None = None, lesson_date: str | None = None, user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    query = select(TeacherLessonNote).where(TeacherLessonNote.school_id == school_id(user))
    if class_id:
        query = query.where(TeacherLessonNote.class_id == class_id)
    if subject_id:
        query = query.where(TeacherLessonNote.subject_id == subject_id)
    if lesson_date:
        query = query.where(TeacherLessonNote.lesson_date == lesson_date)
    result = await session.execute(query.order_by(TeacherLessonNote.lesson_date.desc(), TeacherLessonNote.updated_at.desc()))
    return [item.model_dump() for item in result.scalars().all()]


@router.post("/lesson-notes", response_model=dict)
async def create_lesson_note(payload: TeacherLessonNoteCreate, user: User = Depends(require_permission("academics.lesson_note.manage")), session: AsyncSession = Depends(get_session)):
    teacher_id = await resolve_teacher_id(user, session, payload.teacher_id)
    await assert_teaches_class_subject(user, session, teacher_id, payload.class_id, payload.subject_id)
    await assert_class_campus_access(session, user, payload.class_id)
    await _validate_curriculum_references(session, school_id(user), payload.subject_id, payload.academic_term_id)
    existing_result = await session.execute(select(TeacherLessonNote).where(
        TeacherLessonNote.school_id == school_id(user),
        TeacherLessonNote.teacher_id == teacher_id,
        TeacherLessonNote.class_id == payload.class_id,
        TeacherLessonNote.subject_id == payload.subject_id,
        TeacherLessonNote.lesson_date == payload.lesson_date,
    ))
    item = existing_result.scalar_one_or_none()
    if item:
        item.content = payload.content
        item.academic_term_id = payload.academic_term_id
        item.updated_at = datetime.utcnow()
    else:
        item = TeacherLessonNote(school_id=school_id(user), teacher_id=teacher_id, **payload.model_dump(exclude={"teacher_id"}))
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.patch("/lesson-notes/{note_id}", response_model=dict)
async def update_lesson_note(note_id: str, payload: TeacherLessonNoteUpdate, user: User = Depends(require_permission("academics.lesson_note.manage")), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(TeacherLessonNote).where(TeacherLessonNote.id == note_id, TeacherLessonNote.school_id == school_id(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Lesson note not found")
    await assert_teaches_class_subject(user, session, await teacher_staff_id(user, session), item.class_id, item.subject_id)
    await assert_class_campus_access(session, user, item.class_id)
    item.content = payload.content
    item.updated_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    return item.model_dump()


@router.get("/topics", response_model=list[dict])
async def list_topics(class_id: str | None = None, subject_id: str | None = None, academic_term_id: str | None = None, user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    query = select(CurriculumTopic).where(CurriculumTopic.school_id == school_id(user))
    for field, value in ((CurriculumTopic.class_id, class_id), (CurriculumTopic.subject_id, subject_id), (CurriculumTopic.academic_term_id, academic_term_id)):
        if value:
            query = query.where(field == value)
    result = await session.execute(query.order_by(CurriculumTopic.sequence, CurriculumTopic.planned_week))
    return [item.model_dump() for item in result.scalars().all()]


@router.post("/topics", response_model=dict)
async def create_topic(payload: CurriculumTopicCreate, user: User = Depends(require_permission("academics.topic.manage")), session: AsyncSession = Depends(get_session)):
    if user.role == UserRole.TEACHER:
        await assert_teaches_class_subject(user, session, await teacher_staff_id(user, session), payload.class_id, payload.subject_id)
    await assert_class_campus_access(session, user, payload.class_id)
    await _validate_curriculum_references(session, school_id(user), payload.subject_id, payload.academic_term_id)
    item = CurriculumTopic(school_id=school_id(user), created_by=user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.patch("/topics/{topic_id}", response_model=dict)
async def update_topic(topic_id: str, payload: CurriculumTopicUpdate, user: User = Depends(require_permission("academics.topic.manage")), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(CurriculumTopic).where(CurriculumTopic.id == topic_id, CurriculumTopic.school_id == school_id(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Curriculum topic not found")
    if user.role == UserRole.TEACHER:
        await assert_teaches_class_subject(user, session, await teacher_staff_id(user, session), item.class_id, item.subject_id)
    await assert_class_campus_access(session, user, item.class_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    item.updated_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.patch("/topics/{topic_id}/coverage", response_model=dict)
async def update_topic_coverage(topic_id: str, payload: TopicCoverageUpdate, user: User = Depends(require_permission("academics.topic.manage")), session: AsyncSession = Depends(get_session)):
    if payload.status not in ("planned", "in_progress", "completed", "deferred"):
        raise HTTPException(status_code=422, detail="Invalid coverage status")
    result = await session.execute(select(CurriculumTopic).where(CurriculumTopic.id == topic_id, CurriculumTopic.school_id == school_id(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Curriculum topic not found")
    if user.role == UserRole.TEACHER:
        await assert_teaches_class_subject(user, session, await teacher_staff_id(user, session), item.class_id, item.subject_id)
    await assert_class_campus_access(session, user, item.class_id)
    item.status = payload.status
    item.updated_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    return item.model_dump()


@router.get("/lesson-plans", response_model=list[dict])
async def list_lesson_plans(class_id: str | None = None, subject_id: str | None = None, user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    query = select(LessonPlan).where(LessonPlan.school_id == school_id(user))
    if class_id:
        query = query.where(LessonPlan.class_id == class_id)
    if subject_id:
        query = query.where(LessonPlan.subject_id == subject_id)
    result = await session.execute(query.order_by(LessonPlan.lesson_date.desc()))
    return [item.model_dump() for item in result.scalars().all()]


@router.post("/lesson-plans", response_model=dict)
async def create_lesson_plan(payload: LessonPlanCreate, user: User = Depends(require_permission("academics.lesson_plan.manage")), session: AsyncSession = Depends(get_session)):
    teacher_id = await resolve_teacher_id(user, session, payload.teacher_id)
    await assert_teaches_class_subject(user, session, teacher_id, payload.class_id, payload.subject_id)
    await assert_class_campus_access(session, user, payload.class_id)
    await _validate_curriculum_references(session, school_id(user), payload.subject_id)
    item = LessonPlan(school_id=school_id(user), teacher_id=teacher_id, created_by=user.id, **payload.model_dump(exclude={"teacher_id"}))
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.patch("/lesson-plans/{plan_id}", response_model=dict)
async def update_lesson_plan(plan_id: str, payload: LessonPlanUpdate, user: User = Depends(require_permission("academics.lesson_plan.manage")), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(LessonPlan).where(LessonPlan.id == plan_id, LessonPlan.school_id == school_id(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Lesson plan not found")
    await assert_teaches_class_subject(user, session, await teacher_staff_id(user, session), item.class_id, item.subject_id)
    await assert_class_campus_access(session, user, item.class_id)
    if payload.coverage_status and payload.coverage_status not in ("planned", "in_progress", "completed", "deferred"):
        raise HTTPException(status_code=422, detail="Invalid lesson coverage status")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    if payload.coverage_status == "completed" and item.topic_id:
        topic_result = await session.execute(select(CurriculumTopic).where(CurriculumTopic.id == item.topic_id, CurriculumTopic.school_id == school_id(user)))
        topic = topic_result.scalar_one_or_none()
        if topic:
            topic.status = "completed"
            topic.updated_at = datetime.utcnow()
            session.add(topic)
    item.updated_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    return item.model_dump()


@router.get("/standards", response_model=list[dict])
async def list_standards(subject_id: str | None = None, is_active: bool | None = None, user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    query = select(CurriculumStandard).where(CurriculumStandard.school_id == school_id(user))
    if subject_id:
        query = query.where(CurriculumStandard.subject_id == subject_id)
    if is_active is not None:
        query = query.where(CurriculumStandard.is_active == is_active)
    result = await session.execute(query.order_by(CurriculumStandard.code))
    return [item.model_dump() for item in result.scalars().all()]


@router.post("/standards", response_model=dict)
async def create_standard(payload: CurriculumStandardCreate, user: User = Depends(require_permission("academics.standard.manage")), session: AsyncSession = Depends(get_session)):
    item = CurriculumStandard(school_id=school_id(user), **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.put("/standards/{standard_id}", response_model=dict)
async def update_standard(standard_id: str, payload: CurriculumStandardUpdate, user: User = Depends(require_permission("academics.standard.manage")), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(CurriculumStandard).where(CurriculumStandard.id == standard_id, CurriculumStandard.school_id == school_id(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Curriculum standard not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.delete("/standards/{standard_id}", response_model=dict)
async def delete_standard(standard_id: str, user: User = Depends(require_permission("academics.standard.manage")), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(CurriculumStandard).where(CurriculumStandard.id == standard_id, CurriculumStandard.school_id == school_id(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Curriculum standard not found")
    # TopicStandardLink.standard_id has an ondelete=CASCADE FK, so any links
    # referencing this standard are cleaned up automatically at the DB level.
    await session.delete(item)
    await session.commit()
    return {"message": "Curriculum standard deleted"}


@router.post("/topics/{topic_id}/standards", response_model=list[dict])
async def link_topic_standards(topic_id: str, payload: TopicStandardLinkCreate, user: User = Depends(require_permission("academics.topic_standard.manage")), session: AsyncSession = Depends(get_session)):
    scope = school_id(user)
    topic_result = await session.execute(select(CurriculumTopic).where(CurriculumTopic.id == topic_id, CurriculumTopic.school_id == scope))
    topic = topic_result.scalar_one_or_none()
    if not topic:
        raise HTTPException(status_code=404, detail="Curriculum topic not found")
    if user.role == UserRole.TEACHER:
        await assert_teaches_class_subject(user, session, await teacher_staff_id(user, session), topic.class_id, topic.subject_id)

    if payload.standard_ids:
        found_standards = set((await session.execute(
            select(CurriculumStandard.id).where(CurriculumStandard.id.in_(payload.standard_ids), CurriculumStandard.school_id == scope)
        )).scalars().all())
        missing = set(payload.standard_ids) - found_standards
        if missing:
            raise HTTPException(status_code=400, detail=f"One or more standard_ids do not exist for this school: {', '.join(missing)}")

    existing_result = await session.execute(select(TopicStandardLink).where(TopicStandardLink.topic_id == topic_id, TopicStandardLink.school_id == scope))
    already_linked = {link.standard_id for link in existing_result.scalars().all()}

    created = []
    for standard_id in payload.standard_ids:
        if standard_id in already_linked:
            continue
        link = TopicStandardLink(school_id=scope, topic_id=topic_id, standard_id=standard_id)
        session.add(link)
        created.append(link)
        already_linked.add(standard_id)

    await session.commit()
    for link in created:
        await session.refresh(link)

    all_links_result = await session.execute(select(TopicStandardLink).where(TopicStandardLink.topic_id == topic_id, TopicStandardLink.school_id == scope))
    return [link.model_dump() for link in all_links_result.scalars().all()]


@router.get("/topics/{topic_id}/standards", response_model=list[dict])
async def list_topic_standards(topic_id: str, user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    scope = school_id(user)
    result = await session.execute(select(TopicStandardLink).where(TopicStandardLink.topic_id == topic_id, TopicStandardLink.school_id == scope))
    return [link.model_dump() for link in result.scalars().all()]


@router.delete("/topics/{topic_id}/standards/{standard_id}", response_model=dict)
async def unlink_topic_standard(topic_id: str, standard_id: str, user: User = Depends(require_permission("academics.topic_standard.manage")), session: AsyncSession = Depends(get_session)):
    scope = school_id(user)
    result = await session.execute(select(TopicStandardLink).where(
        TopicStandardLink.topic_id == topic_id, TopicStandardLink.standard_id == standard_id, TopicStandardLink.school_id == scope
    ))
    link = result.scalar_one_or_none()
    if not link:
        raise HTTPException(status_code=404, detail="This standard is not linked to this topic")
    await session.delete(link)
    await session.commit()
    return {"message": "Standard unlinked from topic"}


@router.get("/coverage", response_model=dict)
async def coverage_report(class_id: str | None = None, subject_id: str | None = None, academic_term_id: str | None = None, user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    query = select(CurriculumTopic).where(CurriculumTopic.school_id == school_id(user))
    for field, value in ((CurriculumTopic.class_id, class_id), (CurriculumTopic.subject_id, subject_id), (CurriculumTopic.academic_term_id, academic_term_id)):
        if value:
            query = query.where(field == value)
    topics = (await session.execute(query)).scalars().all()
    total = len(topics)
    completed = sum(1 for topic in topics if topic.status == "completed")
    return {"total_topics": total, "completed_topics": completed, "in_progress_topics": sum(1 for topic in topics if topic.status == "in_progress"), "deferred_topics": sum(1 for topic in topics if topic.status == "deferred"), "coverage_percentage": round(completed / total * 100, 1) if total else 0.0, "topics": [topic.model_dump() for topic in topics]}


@router.get("/coverage/summary", response_model=list[dict])
async def coverage_summary(user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    scope = school_id(user)
    result = await session.execute(select(CurriculumTopic).where(CurriculumTopic.school_id == scope))
    topics = result.scalars().all()
    grouped = {}
    topic_ids_by_key = {}
    completed_topic_ids_by_key = {}
    for topic in topics:
        key = (topic.class_id, topic.subject_id, topic.academic_term_id)
        bucket = grouped.setdefault(key, {"class_id": topic.class_id, "subject_id": topic.subject_id, "academic_term_id": topic.academic_term_id, "total_topics": 0, "completed_topics": 0, "in_progress_topics": 0, "deferred_topics": 0})
        bucket["total_topics"] += 1
        topic_ids_by_key.setdefault(key, set()).add(topic.id)
        if topic.status == "completed":
            bucket["completed_topics"] += 1
            completed_topic_ids_by_key.setdefault(key, set()).add(topic.id)
        elif topic.status == "in_progress":
            bucket["in_progress_topics"] += 1
        elif topic.status == "deferred":
            bucket["deferred_topics"] += 1
    for bucket in grouped.values():
        bucket["coverage_percentage"] = round(bucket["completed_topics"] / bucket["total_topics"] * 100, 1)

    # Additive: standards-based coverage percentage per (class, subject, term)
    # bucket — a standard counts as "covered" once it's linked to at least one
    # completed topic in that bucket. Purely supplementary to the
    # topic-completion coverage_percentage above; that field is untouched.
    all_topic_ids = {tid for ids in topic_ids_by_key.values() for tid in ids}
    links_by_topic: dict = {}
    if all_topic_ids:
        links_result = await session.execute(
            select(TopicStandardLink).where(TopicStandardLink.school_id == scope, TopicStandardLink.topic_id.in_(all_topic_ids))
        )
        for link in links_result.scalars().all():
            links_by_topic.setdefault(link.topic_id, set()).add(link.standard_id)

    for key, bucket in grouped.items():
        linked_standards = set()
        covered_standards = set()
        for tid in topic_ids_by_key.get(key, set()):
            std_ids = links_by_topic.get(tid, set())
            linked_standards |= std_ids
            if tid in completed_topic_ids_by_key.get(key, set()):
                covered_standards |= std_ids
        bucket["standards_linked"] = len(linked_standards)
        bucket["standards_covered"] = len(covered_standards)
        bucket["standards_coverage_percentage"] = round(len(covered_standards) / len(linked_standards) * 100, 1) if linked_standards else None

    return list(grouped.values())


@router.get("/teacher-activity", response_model=dict)
async def teacher_activity(
    class_id: str | None = None,
    subject_id: str | None = None,
    since_date: str | None = None,
    at_risk_days: int = Query(7, ge=1, le=90),
    user: User = Depends(require_permission("academics.curriculum.view")),
    session: AsyncSession = Depends(get_session)
):
    """Per-teacher lesson-note activity: how many notes each teacher has
    logged, how that compares to what their own lesson plans say should
    have happened, and who hasn't logged anything recently. Admin-only —
    this is oversight data about teacher performance, not something every
    authenticated user should see (unlike the plain lesson-notes list).

    "Scheduled" is derived from LessonPlan rows dated on or before today —
    a teacher with no lesson plans at all has no expected activity to
    measure against, so they're never flagged purely for having zero notes.
    academic_term_id isn't filterable here because LessonPlan doesn't carry
    that field; use since_date to bound the window instead.
    """
    sid = school_id(user)
    today = date.today()
    since = date.fromisoformat(since_date) if since_date else (today - timedelta(days=30))

    notes_query = select(TeacherLessonNote).where(
        TeacherLessonNote.school_id == sid, TeacherLessonNote.lesson_date >= since.isoformat()
    )
    plans_query = select(LessonPlan).where(
        LessonPlan.school_id == sid,
        LessonPlan.lesson_date >= since.isoformat(),
        LessonPlan.lesson_date <= today.isoformat(),
    )
    for field_notes, field_plans, value in (
        (TeacherLessonNote.class_id, LessonPlan.class_id, class_id),
        (TeacherLessonNote.subject_id, LessonPlan.subject_id, subject_id),
    ):
        if value:
            notes_query = notes_query.where(field_notes == value)
            plans_query = plans_query.where(field_plans == value)

    notes = (await session.execute(notes_query)).scalars().all()
    plans = (await session.execute(plans_query)).scalars().all()
    note_keys = {(n.teacher_id, n.class_id, n.subject_id, n.lesson_date) for n in notes}

    latest_note_query = (
        select(TeacherLessonNote.teacher_id, func.max(TeacherLessonNote.lesson_date))
        .where(TeacherLessonNote.school_id == sid)
        .group_by(TeacherLessonNote.teacher_id)
    )
    if class_id:
        latest_note_query = latest_note_query.where(TeacherLessonNote.class_id == class_id)
    if subject_id:
        latest_note_query = latest_note_query.where(TeacherLessonNote.subject_id == subject_id)
    latest_by_teacher = {row[0]: row[1] for row in (await session.execute(latest_note_query)).all()}

    teacher_ids = {n.teacher_id for n in notes} | {p.teacher_id for p in plans} | set(latest_by_teacher.keys())

    staff_names = {}
    if teacher_ids:
        staff_rows = (await session.execute(select(Staff).where(Staff.id.in_(teacher_ids)))).scalars().all()
        staff_names = {s.id: f"{s.first_name} {s.last_name}" for s in staff_rows}

    per_teacher = []
    for tid in teacher_ids:
        t_notes = [n for n in notes if n.teacher_id == tid]
        t_plans = [p for p in plans if p.teacher_id == tid]
        scheduled = len(t_plans)
        logged = sum(1 for p in t_plans if (p.teacher_id, p.class_id, p.subject_id, p.lesson_date) in note_keys)
        completion_rate = round(logged / scheduled * 100, 1) if scheduled else None
        last_date = latest_by_teacher.get(tid)
        days_since_last_note = (today - date.fromisoformat(last_date)).days if last_date else None
        at_risk = scheduled > 0 and (days_since_last_note is None or days_since_last_note > at_risk_days)
        per_teacher.append({
            "teacher_id": tid,
            "teacher_name": staff_names.get(tid, tid),
            "notes_logged": len(t_notes),
            "lessons_scheduled": scheduled,
            "lessons_logged": logged,
            "completion_rate": completion_rate,
            "last_note_date": last_date,
            "days_since_last_note": days_since_last_note,
            "at_risk": at_risk,
        })

    per_teacher.sort(key=lambda row: (not row["at_risk"], row["teacher_name"] or ""))
    rates = [row["completion_rate"] for row in per_teacher if row["completion_rate"] is not None]

    return {
        "since_date": since.isoformat(),
        "as_of_date": today.isoformat(),
        "at_risk_days_threshold": at_risk_days,
        "total_teachers": len(per_teacher),
        "at_risk_count": sum(1 for row in per_teacher if row["at_risk"]),
        "average_completion_rate": round(sum(rates) / len(rates), 1) if rates else None,
        "teachers": per_teacher,
    }