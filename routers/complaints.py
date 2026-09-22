"""Parent/student complaint and grievance channel — see models/complaints.py
for why this is separate from the admin-only models.ticket.Ticket system."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user, require_roles
from database import get_session
from models.complaints import (
    Complaint, ComplaintCreate, ComplaintUpdate, ComplaintStatus,
    ComplaintComment, ComplaintCommentCreate,
)
from models.student import Student
from models.user import User, UserRole
from routers.parent import get_parent_children_ids

router = APIRouter(prefix="/complaints", tags=["Complaints"])

STAFF_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR, UserRole.REGISTRAR)
SUBMITTER_ROLES = (UserRole.PARENT, UserRole.STUDENT)


def _school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


def _to_dict(item: Complaint) -> dict:
    return {
        "id": item.id,
        "submitted_by": item.submitted_by,
        "student_id": item.student_id,
        "category": item.category,
        "severity": item.severity,
        "subject": item.subject,
        "description": item.description,
        "status": item.status,
        "assigned_to": item.assigned_to,
        "resolution_notes": item.resolution_notes,
        "resolved_at": item.resolved_at,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


async def _get_or_404(session: AsyncSession, complaint_id: str, school_id: str) -> Complaint:
    item = (await session.execute(select(Complaint).where(Complaint.id == complaint_id, Complaint.school_id == school_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Complaint not found")
    return item


def _can_view(item: Complaint, current_user: User) -> bool:
    return current_user.role in STAFF_ROLES or item.submitted_by == current_user.id


@router.get("", response_model=List[dict])
async def list_complaints(
    status_filter: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    stmt = select(Complaint).where(Complaint.school_id == school_id)
    if current_user.role not in STAFF_ROLES:
        stmt = stmt.where(Complaint.submitted_by == current_user.id)
    if status_filter:
        stmt = stmt.where(Complaint.status == status_filter)
    result = await session.execute(stmt.order_by(Complaint.created_at.desc()))
    return [_to_dict(item) for item in result.scalars().all()]


@router.post("", response_model=dict)
async def create_complaint(
    payload: ComplaintCreate,
    current_user: User = Depends(require_roles(*SUBMITTER_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    if payload.student_id:
        if current_user.role == UserRole.PARENT:
            children = await get_parent_children_ids(current_user, session)
            if payload.student_id not in children:
                raise HTTPException(status_code=403, detail="Not authorized for this student")
        else:
            student = (await session.execute(select(Student).where(Student.id == payload.student_id, Student.user_id == current_user.id))).scalar_one_or_none()
            if not student:
                raise HTTPException(status_code=403, detail="Not authorized for this student")

    item = Complaint(
        school_id=school_id, submitted_by=current_user.id, student_id=payload.student_id,
        category=payload.category.value, severity=payload.severity.value, subject=payload.subject, description=payload.description,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _to_dict(item)


@router.get("/{complaint_id}", response_model=dict)
async def get_complaint(
    complaint_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    item = await _get_or_404(session, complaint_id, school_id)
    if not _can_view(item, current_user):
        raise HTTPException(status_code=403, detail="Not authorized")

    comments_stmt = select(ComplaintComment).where(ComplaintComment.complaint_id == complaint_id)
    if current_user.role not in STAFF_ROLES:
        comments_stmt = comments_stmt.where(ComplaintComment.is_internal == False)  # noqa: E712
    comments = (await session.execute(comments_stmt.order_by(ComplaintComment.created_at))).scalars().all()

    return {
        **_to_dict(item),
        "comments": [
            {"id": c.id, "author_id": c.author_id, "comment": c.comment, "is_internal": c.is_internal, "created_at": c.created_at}
            for c in comments
        ],
    }


@router.post("/{complaint_id}/comments", response_model=dict)
async def add_complaint_comment(
    complaint_id: str,
    payload: ComplaintCommentCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    item = await _get_or_404(session, complaint_id, school_id)
    if not _can_view(item, current_user):
        raise HTTPException(status_code=403, detail="Not authorized")
    is_internal = payload.is_internal and current_user.role in STAFF_ROLES  # a submitter can never post an internal note

    comment = ComplaintComment(complaint_id=complaint_id, author_id=current_user.id, comment=payload.comment, is_internal=is_internal)
    session.add(comment)
    item.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(comment)
    return {"id": comment.id, "author_id": comment.author_id, "comment": comment.comment, "is_internal": comment.is_internal, "created_at": comment.created_at}


@router.patch("/{complaint_id}", response_model=dict)
async def update_complaint(
    complaint_id: str,
    payload: ComplaintUpdate,
    current_user: User = Depends(require_roles(*STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    item = await _get_or_404(session, complaint_id, school_id)
    if payload.status is not None:
        item.status = payload.status.value
        if payload.status in (ComplaintStatus.RESOLVED, ComplaintStatus.DISMISSED):
            item.resolved_at = datetime.utcnow()
    if payload.assigned_to is not None:
        item.assigned_to = payload.assigned_to
    if payload.resolution_notes is not None:
        item.resolution_notes = payload.resolution_notes
    item.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(item)
    return _to_dict(item)
