"""Parent-submitted advance absence notices — distinct from staff marking
attendance after the fact (routers/attendance.py). Approving one does not
write an Attendance row: that stays the teacher's own daily call."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user, require_roles
from database import get_session
from models.parent_requests import AbsenceRequest, AbsenceRequestCreate, AbsenceRequestReview, RequestStatus, AbsenceRequestType
from models.student import Student
from models.user import User, UserRole
from routers.parent import get_parent_children_ids, verify_child_access
from services.plan_gating import require_plan_feature

router = APIRouter(
    prefix="/absence-requests", tags=["Absence Requests"],
    dependencies=[Depends(require_plan_feature("gate_attendance"))],
)

STAFF_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.TEACHER, UserRole.REGISTRAR)


def _school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


def _to_dict(item: AbsenceRequest, student: Optional[Student] = None) -> dict:
    return {
        "id": item.id,
        "student_id": item.student_id,
        "student_name": f"{student.first_name} {student.last_name}" if student else None,
        "requested_by": item.requested_by,
        "request_type": item.request_type,
        "start_date": item.start_date,
        "end_date": item.end_date,
        "expected_arrival_time": item.expected_arrival_time,
        "reason": item.reason,
        "status": item.status,
        "reviewed_by": item.reviewed_by,
        "reviewed_at": item.reviewed_at,
        "review_notes": item.review_notes,
        "created_at": item.created_at,
    }


@router.get("", response_model=List[dict])
async def list_absence_requests(
    status_filter: Optional[str] = None,
    student_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    stmt = select(AbsenceRequest, Student).join(Student, Student.id == AbsenceRequest.student_id).where(AbsenceRequest.school_id == school_id)

    if current_user.role == UserRole.PARENT:
        children = await get_parent_children_ids(current_user, session)
        stmt = stmt.where(AbsenceRequest.student_id.in_(children))
    elif current_user.role not in STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Access denied")

    if status_filter:
        stmt = stmt.where(AbsenceRequest.status == status_filter)
    if student_id:
        stmt = stmt.where(AbsenceRequest.student_id == student_id)

    result = await session.execute(stmt.order_by(AbsenceRequest.created_at.desc()))
    return [_to_dict(item, student) for item, student in result.all()]


@router.post("", response_model=dict)
async def create_absence_request(
    payload: AbsenceRequestCreate,
    current_user: User = Depends(require_roles(UserRole.PARENT)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    student = await verify_child_access(payload.student_id, current_user, session)
    if payload.end_date < payload.start_date:
        raise HTTPException(status_code=422, detail="end_date cannot be before start_date")
    if payload.request_type == AbsenceRequestType.LATE_ARRIVAL and not payload.expected_arrival_time:
        raise HTTPException(status_code=422, detail="expected_arrival_time is required for a late-arrival request")

    item = AbsenceRequest(school_id=school_id, requested_by=current_user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _to_dict(item, student)


@router.post("/{request_id}/cancel", response_model=dict)
async def cancel_absence_request(
    request_id: str,
    current_user: User = Depends(require_roles(UserRole.PARENT)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    item = (await session.execute(select(AbsenceRequest).where(AbsenceRequest.id == request_id, AbsenceRequest.school_id == school_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Absence request not found")
    if item.requested_by != current_user.id:
        raise HTTPException(status_code=403, detail="You can only cancel your own requests")
    if item.status != RequestStatus.PENDING.value:
        raise HTTPException(status_code=400, detail="Only a pending request can be cancelled")
    item.status = RequestStatus.CANCELLED.value
    item.updated_at = datetime.utcnow()
    await session.commit()
    return {"id": item.id, "status": item.status}


@router.post("/{request_id}/review", response_model=dict)
async def review_absence_request(
    request_id: str,
    payload: AbsenceRequestReview,
    current_user: User = Depends(require_roles(*STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    result = await session.execute(
        select(AbsenceRequest, Student).join(Student, Student.id == AbsenceRequest.student_id).where(AbsenceRequest.id == request_id, AbsenceRequest.school_id == school_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Absence request not found")
    item, student = row
    if item.status != RequestStatus.PENDING.value:
        raise HTTPException(status_code=400, detail="This request has already been reviewed")
    if payload.status not in (RequestStatus.APPROVED, RequestStatus.REJECTED):
        raise HTTPException(status_code=422, detail="A review must approve or reject the request")

    item.status = payload.status.value
    item.reviewed_by = current_user.id
    item.reviewed_at = datetime.utcnow()
    item.review_notes = payload.review_notes
    item.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(item)
    return _to_dict(item, student)
