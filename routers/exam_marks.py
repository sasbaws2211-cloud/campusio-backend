"""Exam component definitions + marks capture — see models/exam_marks.py.
Components (Paper 1/2, practical/theory, ...) belong to an ExamSchedule
sitting; marks are captured per (component, student) and stay hidden from
students/parents until the owning ExamSession is published
(models/exam.py::ExamSession.results_published, flipped either manually
via routers/exams.py or automatically by
services/scheduler.py::run_exam_result_auto_publish).

A component with no linked ExamPaper is the informal path (no paper-bank
authoring used for this sitting) and can always receive marks. A component
LINKED to an ExamPaper (routers/exam_papers.py's maker-checker workflow)
can only receive marks once that paper is APPROVED — see upsert_marks —
so the moderation step can't be silently skipped for exams that do use it."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user, require_permission
from database import get_session
from models.exam import ExamSchedule, ExamSession
from models.exam_marks import (
    ExamComponent, ExamComponentCreate, ExamComponentUpdate,
    ExamComponentMark, ExamComponentMarkUpsert, BulkExamComponentMarksUpsert,
)
from models.exam_papers import ExamPaper, ExamPaperStatus
from models.staff import Staff, TeacherAssignment
from models.student import Student
from models.user import User, UserRole
from routers.parent import get_parent_children_ids, verify_child_access

router = APIRouter(prefix="/exam-marks", tags=["Exam Marks"])

WRITE_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.TEACHER)


def _school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


async def _get_schedule_or_404(session: AsyncSession, school_id: str, schedule_id: str) -> ExamSchedule:
    schedule = (await session.execute(select(ExamSchedule).where(ExamSchedule.id == schedule_id, ExamSchedule.school_id == school_id))).scalar_one_or_none()
    if not schedule:
        raise HTTPException(status_code=404, detail="Exam schedule not found")
    return schedule


async def _get_component_or_404(session: AsyncSession, school_id: str, component_id: str) -> ExamComponent:
    component = (await session.execute(select(ExamComponent).where(ExamComponent.id == component_id, ExamComponent.school_id == school_id))).scalar_one_or_none()
    if not component:
        raise HTTPException(status_code=404, detail="Exam component not found")
    return component


async def _session_for_component(session: AsyncSession, component: ExamComponent) -> Optional[ExamSession]:
    row = (await session.execute(
        select(ExamSession).join(ExamSchedule, ExamSchedule.exam_session_id == ExamSession.id)
        .where(ExamSchedule.id == component.exam_schedule_id)
    )).scalar_one_or_none()
    return row


def _results_visible(exam_session: Optional[ExamSession]) -> bool:
    return bool(exam_session and exam_session.results_published)


async def _own_staff_id(user: User, session: AsyncSession) -> Optional[str]:
    result = await session.execute(select(Staff).where(Staff.school_id == _school_id(user), Staff.user_id == user.id))
    staff = result.scalar_one_or_none()
    if not staff:
        result = await session.execute(select(Staff).where(Staff.school_id == _school_id(user), Staff.email == user.email))
        staff = result.scalar_one_or_none()
    return staff.id if staff else None


async def _assert_teaches_schedule(session: AsyncSession, user: User, schedule: ExamSchedule) -> None:
    """exams.component.manage/exams.marks.manage/exams.marks.view are broad,
    school-wide RBAC grants (same shape as academics.lesson_note.manage etc.
    in routers/curriculum.py) — without this, any teacher could create/edit
    exam components, record marks, or view a whole class's marks for a
    class/subject they've never been assigned to teach. No-op for admins,
    who legitimately manage exams school-wide."""
    if user.role != UserRole.TEACHER:
        return
    staff_id = await _own_staff_id(user, session)
    if not staff_id:
        raise HTTPException(status_code=403, detail="Teacher staff profile not found")
    result = await session.execute(select(TeacherAssignment).where(
        TeacherAssignment.school_id == _school_id(user),
        TeacherAssignment.staff_id == staff_id,
        TeacherAssignment.class_id == schedule.class_id,
        TeacherAssignment.subject_id == schedule.subject_id,
    ))
    if not result.scalars().first():
        raise HTTPException(status_code=403, detail="You are not assigned to teach this class/subject")


async def _assert_teaches_component(session: AsyncSession, user: User, component: ExamComponent) -> None:
    schedule = await session.get(ExamSchedule, component.exam_schedule_id)
    if schedule:
        await _assert_teaches_schedule(session, user, schedule)


# ── Components ───────────────────────────────────────────────────────────

@router.post("/schedules/{schedule_id}/components", response_model=dict)
async def create_component(
    schedule_id: str,
    data: ExamComponentCreate,
    current_user: User = Depends(require_permission("exams.component.manage")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    schedule = await _get_schedule_or_404(session, school_id, schedule_id)
    await _assert_teaches_schedule(session, current_user, schedule)

    if data.exam_paper_id:
        paper = (await session.execute(
            select(ExamPaper).where(ExamPaper.id == data.exam_paper_id, ExamPaper.school_id == school_id)
        )).scalar_one_or_none()
        if not paper:
            raise HTTPException(status_code=404, detail="Exam paper not found")
        if paper.exam_schedule_id and paper.exam_schedule_id != schedule_id:
            raise HTTPException(status_code=400, detail="This exam paper is linked to a different exam schedule")

    component = ExamComponent(school_id=school_id, exam_schedule_id=schedule_id, **data.model_dump())
    session.add(component)
    await session.commit()
    await session.refresh(component)
    return jsonable_encoder(component)


@router.get("/schedules/{schedule_id}/components", response_model=List[dict])
async def list_components(
    schedule_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    await _get_schedule_or_404(session, school_id, schedule_id)
    result = await session.execute(select(ExamComponent).where(ExamComponent.exam_schedule_id == schedule_id).order_by(ExamComponent.order_index))
    return [jsonable_encoder(c) for c in result.scalars().all()]


@router.put("/components/{component_id}", response_model=dict)
async def update_component(
    component_id: str,
    data: ExamComponentUpdate,
    current_user: User = Depends(require_permission("exams.component.manage")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    component = await _get_component_or_404(session, school_id, component_id)
    await _assert_teaches_component(session, current_user, component)
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(component, key, value)
    session.add(component)
    await session.commit()
    await session.refresh(component)
    return jsonable_encoder(component)


@router.delete("/components/{component_id}", response_model=dict)
async def delete_component(
    component_id: str,
    current_user: User = Depends(require_permission("exams.component.manage")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    component = await _get_component_or_404(session, school_id, component_id)
    await _assert_teaches_component(session, current_user, component)
    await session.delete(component)
    await session.commit()
    return {"success": True, "message": "Exam component deleted"}


# ── Marks ────────────────────────────────────────────────────────────────

def _mark_to_dict(mark: ExamComponentMark, student: Optional[Student] = None) -> dict:
    return {
        "id": mark.id,
        "exam_component_id": mark.exam_component_id,
        "student_id": mark.student_id,
        "student_name": f"{student.first_name} {student.last_name}" if student else None,
        "score": mark.score,
        "remarks": mark.remarks,
        "annulled": mark.annulled,
        "recorded_by": mark.recorded_by,
        "created_at": mark.created_at,
        "updated_at": mark.updated_at,
    }


@router.post("/components/{component_id}/marks/bulk", response_model=dict)
async def upsert_marks(
    component_id: str,
    data: BulkExamComponentMarksUpsert,
    current_user: User = Depends(require_permission("exams.marks.manage")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    component = await _get_component_or_404(session, school_id, component_id)
    await _assert_teaches_component(session, current_user, component)

    # A component linked to a formal exam paper can't receive marks until
    # that paper has cleared the maker-checker moderation workflow
    # (routers/exam_papers.py) — otherwise a teacher could record marks
    # against a paper that was never actually submitted or approved. A
    # component with no linked paper at all (the informal/no-paper-bank
    # path) is unaffected — there's nothing to moderate in that case.
    if component.exam_paper_id:
        paper = await session.get(ExamPaper, component.exam_paper_id)
        if paper and paper.status != ExamPaperStatus.APPROVED.value:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot record marks — the linked exam paper has not been approved yet (status: {paper.status}). Submit and approve it first, or unlink it from this component.",
            )

    # ExamComponentMark.student_id has no DB-level foreign key, so without this
    # check a mark could be recorded against a student from a different school
    # (or an id that doesn't exist at all) and would be silently persisted as
    # a cross-tenant orphan record — same gap validate_grade_references closes
    # for Grade in routers/grades.py.
    submitted_student_ids = {entry.student_id for entry in data.marks}
    if submitted_student_ids:
        found_student_ids = set((await session.execute(
            select(Student.id).where(Student.id.in_(submitted_student_ids), Student.school_id == school_id)
        )).scalars().all())
        missing = submitted_student_ids - found_student_ids
        if missing:
            raise HTTPException(status_code=400, detail=f"One or more student_id values do not exist for this school: {sorted(missing)}")

    existing_result = await session.execute(select(ExamComponentMark).where(ExamComponentMark.exam_component_id == component_id))
    existing_by_student = {m.student_id: m for m in existing_result.scalars().all()}

    created, updated = 0, 0
    for entry in data.marks:
        if entry.score < 0 or entry.score > component.max_marks:
            raise HTTPException(status_code=422, detail=f"Score for student {entry.student_id} must be between 0 and {component.max_marks}")
        existing = existing_by_student.get(entry.student_id)
        if existing:
            existing.score = entry.score
            existing.remarks = entry.remarks
            existing.recorded_by = current_user.id
            existing.updated_at = datetime.utcnow()
            session.add(existing)
            updated += 1
        else:
            session.add(ExamComponentMark(
                school_id=school_id, exam_component_id=component_id, student_id=entry.student_id,
                score=entry.score, remarks=entry.remarks, recorded_by=current_user.id,
            ))
            created += 1

    await session.commit()
    return {"success": True, "created": created, "updated": updated}


@router.get("/components/{component_id}/marks", response_model=List[dict])
async def list_marks(
    component_id: str,
    current_user: User = Depends(require_permission("exams.marks.view")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    component = await _get_component_or_404(session, school_id, component_id)
    await _assert_teaches_component(session, current_user, component)
    result = await session.execute(
        select(ExamComponentMark, Student).join(Student, Student.id == ExamComponentMark.student_id)
        .where(ExamComponentMark.exam_component_id == component_id)
    )
    return [_mark_to_dict(mark, student) for mark, student in result.all()]


@router.get("/students/{student_id}/marks", response_model=List[dict])
async def list_student_marks(
    student_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Staff see everything, including unpublished sessions. A parent/
    student only ever sees marks whose owning ExamSession has been
    published — same release-gate the report-card system already uses."""
    school_id = _school_id(current_user)
    is_staff = current_user.role in WRITE_ROLES or current_user.role in (UserRole.REGISTRAR,)

    if current_user.role == UserRole.PARENT:
        await verify_child_access(student_id, current_user, session)
    elif current_user.role == UserRole.STUDENT:
        student = (await session.execute(select(Student).where(Student.id == student_id, Student.user_id == current_user.id))).scalar_one_or_none()
        if not student:
            raise HTTPException(status_code=404, detail="Student not found or access denied")
    elif not is_staff:
        raise HTTPException(status_code=403, detail="Access denied")

    result = await session.execute(
        select(ExamComponentMark, ExamComponent, ExamSchedule, ExamSession)
        .join(ExamComponent, ExamComponent.id == ExamComponentMark.exam_component_id)
        .join(ExamSchedule, ExamSchedule.id == ExamComponent.exam_schedule_id)
        .join(ExamSession, ExamSession.id == ExamSchedule.exam_session_id)
        .where(ExamComponentMark.student_id == student_id, ExamComponentMark.school_id == school_id)
        .order_by(ExamSchedule.exam_date.desc())
    )
    rows = result.all()
    if not is_staff:
        rows = [row for row in rows if row[3].results_published]

    return [
        {
            "id": mark.id,
            "exam_component_id": component.id,
            "component_name": component.name,
            "max_marks": component.max_marks,
            "score": mark.score,
            "annulled": mark.annulled,
            "exam_schedule_id": schedule.id,
            "subject_id": schedule.subject_id,
            "exam_date": schedule.exam_date,
            "exam_session_id": exam_session.id,
            "exam_session_name": exam_session.name,
            "results_published": exam_session.results_published,
        }
        for mark, component, schedule, exam_session in rows
    ]
