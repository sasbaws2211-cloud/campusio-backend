"""Academic-integrity / exam malpractice case tracking — see
models/exam_malpractice.py. Staff-only (mirrors models/discipline.py
having no parent-facing view). Resolving a case with a result-annulling
sanction against a specific component flips that student's
ExamComponentMark.annulled — see resolve_case below."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_permission
from database import get_session
from models.exam import ExamSchedule, ExamSession
from models.exam_malpractice import (
    MalpracticeCase, MalpracticeCaseCreate, MalpracticeCaseUpdate,
    MalpracticeStatus, MalpracticeSanction,
)
from models.exam_marks import ExamComponent, ExamComponentMark
from models.student import Student
from models.user import User
from services.exam_result_aggregation_service import reaggregate_and_flag_report_card

router = APIRouter(prefix="/malpractice-cases", tags=["Exam Malpractice"])


def _school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


def _to_dict(item: MalpracticeCase, student: Optional[Student] = None) -> dict:
    return {
        "id": item.id,
        "exam_schedule_id": item.exam_schedule_id,
        "student_id": item.student_id,
        "student_name": f"{student.first_name} {student.last_name}" if student else None,
        "exam_component_id": item.exam_component_id,
        "reported_by": item.reported_by,
        "category": item.category,
        "description": item.description,
        "status": item.status,
        "investigation_notes": item.investigation_notes,
        "sanction": item.sanction,
        "sanction_notes": item.sanction_notes,
        "resolved_by": item.resolved_by,
        "resolved_at": item.resolved_at,
        "created_at": item.created_at,
    }


@router.post("", response_model=dict)
async def create_case(
    payload: MalpracticeCaseCreate,
    current_user: User = Depends(require_permission("exams.malpractice.create")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    schedule = (await session.execute(select(ExamSchedule).where(ExamSchedule.id == payload.exam_schedule_id, ExamSchedule.school_id == school_id))).scalar_one_or_none()
    if not schedule:
        raise HTTPException(status_code=404, detail="Exam schedule not found")
    student = (await session.execute(select(Student).where(Student.id == payload.student_id, Student.school_id == school_id))).scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    # exam_component_id was never validated -- a garbage/mismatched id
    # silently persisted, undetected until update_case's later annulment
    # lookup just quietly fails to find a matching mark. Must belong to
    # this same exam_schedule_id, not just exist somewhere for the school.
    if payload.exam_component_id:
        component = (await session.execute(
            select(ExamComponent).where(
                ExamComponent.id == payload.exam_component_id,
                ExamComponent.exam_schedule_id == payload.exam_schedule_id,
                ExamComponent.school_id == school_id,
            )
        )).scalar_one_or_none()
        if not component:
            raise HTTPException(status_code=404, detail="Exam component not found for this exam schedule")

    item = MalpracticeCase(
        school_id=school_id, reported_by=current_user.id,
        exam_schedule_id=payload.exam_schedule_id, student_id=payload.student_id,
        exam_component_id=payload.exam_component_id, category=payload.category.value, description=payload.description,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _to_dict(item, student)


@router.get("", response_model=List[dict])
async def list_cases(
    status_filter: Optional[str] = None,
    exam_schedule_id: Optional[str] = None,
    student_id: Optional[str] = None,
    current_user: User = Depends(require_permission("exams.malpractice.view")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    stmt = select(MalpracticeCase, Student).join(Student, Student.id == MalpracticeCase.student_id).where(MalpracticeCase.school_id == school_id)
    if status_filter:
        stmt = stmt.where(MalpracticeCase.status == status_filter)
    if exam_schedule_id:
        stmt = stmt.where(MalpracticeCase.exam_schedule_id == exam_schedule_id)
    if student_id:
        stmt = stmt.where(MalpracticeCase.student_id == student_id)
    result = await session.execute(stmt.order_by(MalpracticeCase.created_at.desc()))
    return [_to_dict(item, student) for item, student in result.all()]


@router.get("/{case_id}", response_model=dict)
async def get_case(
    case_id: str,
    current_user: User = Depends(require_permission("exams.malpractice.view")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    row = (await session.execute(select(MalpracticeCase, Student).join(Student, Student.id == MalpracticeCase.student_id).where(MalpracticeCase.id == case_id, MalpracticeCase.school_id == school_id))).first()
    if not row:
        raise HTTPException(status_code=404, detail="Case not found")
    item, student = row
    return _to_dict(item, student)


@router.patch("/{case_id}", response_model=dict)
async def update_case(
    case_id: str,
    payload: MalpracticeCaseUpdate,
    current_user: User = Depends(require_permission("exams.malpractice.resolve")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    row = (await session.execute(select(MalpracticeCase, Student).join(Student, Student.id == MalpracticeCase.student_id).where(MalpracticeCase.id == case_id, MalpracticeCase.school_id == school_id))).first()
    if not row:
        raise HTTPException(status_code=404, detail="Case not found")
    item, student = row

    if payload.investigation_notes is not None:
        item.investigation_notes = payload.investigation_notes
    if payload.sanction is not None:
        item.sanction = payload.sanction.value
    if payload.sanction_notes is not None:
        item.sanction_notes = payload.sanction_notes

    annul_mark = False
    if payload.status is not None:
        if payload.status in (MalpracticeStatus.RESOLVED, MalpracticeStatus.DISMISSED) and item.reported_by == current_user.id:
            # Same maker-checker gap already closed for exam papers
            # (routers/exam_papers.py::approve_paper/reject_paper) — without
            # this, the same person who reported a case (possibly against a
            # student they have a conflict with) could immediately resolve
            # it themselves, including annulling that student's result, with
            # no independent review at all.
            raise HTTPException(status_code=403, detail="You cannot resolve a case you reported — ask another staff member to review it")
        item.status = payload.status.value
        if payload.status in (MalpracticeStatus.RESOLVED, MalpracticeStatus.DISMISSED):
            item.resolved_by = current_user.id
            item.resolved_at = datetime.utcnow()
            if payload.status == MalpracticeStatus.RESOLVED and item.sanction == MalpracticeSanction.RESULT_ANNULLED.value and item.exam_component_id:
                mark = (await session.execute(select(ExamComponentMark).where(ExamComponentMark.exam_component_id == item.exam_component_id, ExamComponentMark.student_id == item.student_id))).scalar_one_or_none()
                if mark:
                    mark.annulled = True
                    mark.updated_at = datetime.utcnow()
                    session.add(mark)
                    annul_mark = True

    item.updated_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    await session.refresh(item)

    result = _to_dict(item, student)
    if annul_mark:
        schedule = await session.get(ExamSchedule, item.exam_schedule_id)
        exam_session = await session.get(ExamSession, schedule.exam_session_id) if schedule else None
        if exam_session and exam_session.results_published:
            # Only re-aggregate if results were already published — before
            # that, no Grade row exists yet for this session at all (the
            # eventual publish will aggregate marks with this one already
            # correctly excluded, since aggregate_exam_session_to_grades
            # skips annulled marks).
            result["grade_reaggregation"] = await reaggregate_and_flag_report_card(
                session, exam_session, item.student_id, actor_id=current_user.id
            )
    return result
