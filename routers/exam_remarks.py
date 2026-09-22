"""Student/parent-initiated exam recheck/remark requests — see
models/exam_remarks.py. A request can only be raised against a published
result (the requester has to have actually seen the score to contest it),
and a REVISED outcome writes the new score back onto the underlying
ExamComponentMark."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user, require_permission
from database import get_session
from models.exam import ExamSchedule, ExamSession
from models.exam_marks import ExamComponent, ExamComponentMark
from models.exam_remarks import ExamRemarkRequest, ExamRemarkRequestCreate, ExamRemarkRequestReview, RemarkRequestStatus
from models.student import Student
from models.user import User, UserRole
from routers.parent import get_parent_children_ids, verify_child_access
from services.exam_result_aggregation_service import reaggregate_and_flag_report_card

router = APIRouter(prefix="/exam-remarks", tags=["Exam Remark Requests"])

STAFF_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.TEACHER, UserRole.REGISTRAR)


def _school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


def _to_dict(item: ExamRemarkRequest, student: Optional[Student] = None, component: Optional[ExamComponent] = None) -> dict:
    return {
        "id": item.id,
        "student_id": item.student_id,
        "student_name": f"{student.first_name} {student.last_name}" if student else None,
        "exam_component_id": item.exam_component_id,
        "component_name": component.name if component else None,
        "requested_by": item.requested_by,
        "reason": item.reason,
        "original_score": item.original_score,
        "revised_score": item.revised_score,
        "status": item.status,
        "reviewed_by": item.reviewed_by,
        "reviewed_at": item.reviewed_at,
        "review_notes": item.review_notes,
        "created_at": item.created_at,
    }


@router.get("", response_model=List[dict])
async def list_remark_requests(
    status_filter: Optional[str] = None,
    student_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    stmt = (
        select(ExamRemarkRequest, Student, ExamComponent)
        .join(Student, Student.id == ExamRemarkRequest.student_id)
        .join(ExamComponent, ExamComponent.id == ExamRemarkRequest.exam_component_id)
        .where(ExamRemarkRequest.school_id == school_id)
    )
    if current_user.role == UserRole.PARENT:
        children = await get_parent_children_ids(current_user, session)
        stmt = stmt.where(ExamRemarkRequest.student_id.in_(children))
    elif current_user.role == UserRole.STUDENT:
        stmt = stmt.where(ExamRemarkRequest.requested_by == current_user.id)
    elif current_user.role not in STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Access denied")

    if status_filter:
        stmt = stmt.where(ExamRemarkRequest.status == status_filter)
    if student_id:
        stmt = stmt.where(ExamRemarkRequest.student_id == student_id)

    result = await session.execute(stmt.order_by(ExamRemarkRequest.created_at.desc()))
    return [_to_dict(item, student, component) for item, student, component in result.all()]


@router.post("", response_model=dict)
async def create_remark_request(
    payload: ExamRemarkRequestCreate,
    current_user: User = Depends(require_permission("exams.remark.request")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    if current_user.role == UserRole.PARENT:
        student = await verify_child_access(payload.student_id, current_user, session)
    else:
        student = (await session.execute(select(Student).where(Student.id == payload.student_id, Student.user_id == current_user.id))).scalar_one_or_none()
        if not student:
            raise HTTPException(status_code=404, detail="Student not found or access denied")

    row = (await session.execute(
        select(ExamComponentMark, ExamComponent, ExamSession)
        .join(ExamComponent, ExamComponent.id == ExamComponentMark.exam_component_id)
        .join(ExamSchedule, ExamSchedule.id == ExamComponent.exam_schedule_id)
        .join(ExamSession, ExamSession.id == ExamSchedule.exam_session_id)
        .where(ExamComponentMark.exam_component_id == payload.exam_component_id, ExamComponentMark.student_id == payload.student_id)
    )).first()
    if not row:
        raise HTTPException(status_code=404, detail="No recorded mark found for this student on this component")
    mark, component, exam_session = row
    if not exam_session.results_published:
        raise HTTPException(status_code=400, detail="Results for this exam have not been published yet")

    already = (await session.execute(
        select(ExamRemarkRequest).where(
            ExamRemarkRequest.exam_component_id == payload.exam_component_id,
            ExamRemarkRequest.student_id == payload.student_id,
            ExamRemarkRequest.status.in_([RemarkRequestStatus.PENDING.value, RemarkRequestStatus.UNDER_REVIEW.value]),
        )
    )).scalar_one_or_none()
    if already:
        raise HTTPException(status_code=409, detail="A remark request for this result is already in progress")

    item = ExamRemarkRequest(
        school_id=school_id, student_id=payload.student_id, exam_component_id=payload.exam_component_id,
        requested_by=current_user.id, reason=payload.reason, original_score=mark.score,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _to_dict(item, student, component)


@router.post("/{request_id}/cancel", response_model=dict)
async def cancel_remark_request(
    request_id: str,
    current_user: User = Depends(require_permission("exams.remark.request")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    item = (await session.execute(select(ExamRemarkRequest).where(ExamRemarkRequest.id == request_id, ExamRemarkRequest.school_id == school_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Remark request not found")
    if item.requested_by != current_user.id:
        raise HTTPException(status_code=403, detail="You can only cancel your own requests")
    if item.status not in (RemarkRequestStatus.PENDING.value, RemarkRequestStatus.UNDER_REVIEW.value):
        raise HTTPException(status_code=400, detail=f"Cannot cancel a request that is {item.status}")
    item.status = RemarkRequestStatus.REJECTED.value
    item.review_notes = "Cancelled by requester"
    item.updated_at = datetime.utcnow()
    await session.commit()
    return {"id": item.id, "status": item.status}


@router.post("/{request_id}/review", response_model=dict)
async def review_remark_request(
    request_id: str,
    payload: ExamRemarkRequestReview,
    current_user: User = Depends(require_permission("exams.remark.review")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    row = (await session.execute(
        select(ExamRemarkRequest, Student, ExamComponent)
        .join(Student, Student.id == ExamRemarkRequest.student_id)
        .join(ExamComponent, ExamComponent.id == ExamRemarkRequest.exam_component_id)
        .where(ExamRemarkRequest.id == request_id, ExamRemarkRequest.school_id == school_id)
    )).first()
    if not row:
        raise HTTPException(status_code=404, detail="Remark request not found")
    item, student, component = row
    if item.status not in (RemarkRequestStatus.PENDING.value, RemarkRequestStatus.UNDER_REVIEW.value):
        raise HTTPException(status_code=400, detail=f"This request has already been closed ({item.status})")

    if payload.status == RemarkRequestStatus.REVISED:
        if payload.revised_score is None:
            raise HTTPException(status_code=422, detail="revised_score is required to mark a request as revised")
        if payload.revised_score < 0 or payload.revised_score > component.max_marks:
            raise HTTPException(status_code=422, detail=f"revised_score must be between 0 and {component.max_marks}")
        mark = (await session.execute(select(ExamComponentMark).where(ExamComponentMark.exam_component_id == item.exam_component_id, ExamComponentMark.student_id == item.student_id))).scalar_one_or_none()
        if mark:
            mark.score = payload.revised_score
            mark.updated_at = datetime.utcnow()
            session.add(mark)
        item.revised_score = payload.revised_score

    item.status = payload.status.value
    item.reviewed_by = current_user.id
    item.reviewed_at = datetime.utcnow()
    item.review_notes = payload.review_notes
    item.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(item)

    result = _to_dict(item, student, component)
    if payload.status == RemarkRequestStatus.REVISED:
        # A remark can only ever be requested against an already-published
        # session (create_remark_request enforces this), so results are
        # always published by the time a review reaches here — re-aggregate
        # so the revised mark actually reaches the student's Grade/report
        # card instead of silently sitting only on ExamComponentMark.
        schedule = await session.get(ExamSchedule, component.exam_schedule_id)
        exam_session = await session.get(ExamSession, schedule.exam_session_id) if schedule else None
        if exam_session:
            result["grade_reaggregation"] = await reaggregate_and_flag_report_card(
                session, exam_session, item.student_id, actor_id=current_user.id
            )
    return result
