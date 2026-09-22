"""Generic 'parent must confirm they've seen this' tracking — see
models/acknowledgements.py. Opt-in: staff explicitly requests
acknowledgement for a specific subject from specific target users, rather
than every announcement/report card silently requiring one."""
from __future__ import annotations

from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user, require_roles
from database import get_session
from models.acknowledgements import Acknowledgement, AcknowledgementRequestCreate
from models.user import User, UserRole

router = APIRouter(prefix="/acknowledgements", tags=["Acknowledgements"])

STAFF_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.TEACHER, UserRole.REGISTRAR)


def _school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


def _to_dict(item: Acknowledgement) -> dict:
    return {
        "id": item.id,
        "subject_type": item.subject_type,
        "subject_id": item.subject_id,
        "title": item.title,
        "target_user_id": item.target_user_id,
        "student_id": item.student_id,
        "acknowledged": item.acknowledged,
        "acknowledged_at": item.acknowledged_at,
        "required_by": item.required_by,
        "requested_by": item.requested_by,
        "created_at": item.created_at,
    }


@router.post("/request", response_model=dict)
async def request_acknowledgements(
    payload: AcknowledgementRequestCreate,
    current_user: User = Depends(require_roles(*STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    if not payload.target_user_ids:
        raise HTTPException(status_code=422, detail="At least one target user is required")

    created = []
    for user_id in set(payload.target_user_ids):
        item = Acknowledgement(
            school_id=school_id, subject_type=payload.subject_type.value, subject_id=payload.subject_id,
            title=payload.title, target_user_id=user_id, student_id=payload.student_id,
            required_by=payload.required_by, requested_by=current_user.id,
        )
        session.add(item)
        created.append(item)
    await session.commit()
    for item in created:
        await session.refresh(item)
    return {"requested_count": len(created), "items": [_to_dict(item) for item in created]}


@router.get("/subject/{subject_type}/{subject_id}", response_model=List[dict])
async def get_subject_acknowledgements(
    subject_type: str,
    subject_id: str,
    current_user: User = Depends(require_roles(*STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    result = await session.execute(
        select(Acknowledgement).where(
            Acknowledgement.school_id == school_id, Acknowledgement.subject_type == subject_type, Acknowledgement.subject_id == subject_id
        )
    )
    return [_to_dict(item) for item in result.scalars().all()]


@router.get("/mine", response_model=List[dict])
async def list_my_acknowledgements(
    pending_only: bool = False,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    stmt = select(Acknowledgement).where(Acknowledgement.school_id == school_id, Acknowledgement.target_user_id == current_user.id)
    if pending_only:
        stmt = stmt.where(Acknowledgement.acknowledged == False)  # noqa: E712
    result = await session.execute(stmt.order_by(Acknowledgement.created_at.desc()))
    return [_to_dict(item) for item in result.scalars().all()]


@router.post("/{acknowledgement_id}/acknowledge", response_model=dict)
async def acknowledge(
    acknowledgement_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    item = (
        await session.execute(select(Acknowledgement).where(Acknowledgement.id == acknowledgement_id, Acknowledgement.school_id == school_id))
    ).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Acknowledgement not found")
    if item.target_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized")
    if not item.acknowledged:
        item.acknowledged = True
        item.acknowledged_at = datetime.utcnow()
        await session.commit()
        await session.refresh(item)
    return _to_dict(item)
