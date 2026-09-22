"""Strategic goal / OKR tracking — see models/strategic_goals.py."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_roles
from database import get_session
from models.strategic_goals import (
    StrategicGoal, StrategicGoalCreate, StrategicGoalUpdate, StrategicGoalProgressUpdate,
)
from models.user import User, UserRole

router = APIRouter(prefix="/strategic-goals", tags=["Strategic Goals"])

STAFF_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR)


def _school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


def _to_dict(item: StrategicGoal) -> dict:
    progress_pct = None
    if item.target_value is not None and item.current_value is not None and item.target_value != 0:
        progress_pct = round(item.current_value / item.target_value * 100, 1)
    return {
        "id": item.id, "title": item.title, "description": item.description, "category": item.category,
        "target_metric": item.target_metric, "target_value": item.target_value, "current_value": item.current_value,
        "unit": item.unit, "start_date": item.start_date, "target_date": item.target_date, "status": item.status,
        "owner_id": item.owner_id, "progress_pct": progress_pct, "created_at": item.created_at,
    }


@router.post("", response_model=dict)
async def create_goal(payload: StrategicGoalCreate, current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    item = StrategicGoal(school_id=school_id, created_by=current_user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _to_dict(item)


@router.get("", response_model=List[dict])
async def list_goals(
    status_filter: Optional[str] = None, category: Optional[str] = None,
    current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    stmt = select(StrategicGoal).where(StrategicGoal.school_id == school_id)
    if status_filter:
        stmt = stmt.where(StrategicGoal.status == status_filter)
    if category:
        stmt = stmt.where(StrategicGoal.category == category)
    result = await session.execute(stmt.order_by(StrategicGoal.target_date))
    return [_to_dict(g) for g in result.scalars().all()]


@router.put("/{goal_id}", response_model=dict)
async def update_goal(goal_id: str, payload: StrategicGoalUpdate, current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    item = (await session.execute(select(StrategicGoal).where(StrategicGoal.id == goal_id, StrategicGoal.school_id == school_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Goal not found")
    update_data = payload.model_dump(exclude_unset=True)
    if "status" in update_data and update_data["status"] is not None:
        update_data["status"] = update_data["status"].value
    for key, value in update_data.items():
        setattr(item, key, value)
    item.updated_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _to_dict(item)


@router.post("/{goal_id}/progress", response_model=dict)
async def update_progress(goal_id: str, payload: StrategicGoalProgressUpdate, current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    item = (await session.execute(select(StrategicGoal).where(StrategicGoal.id == goal_id, StrategicGoal.school_id == school_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Goal not found")
    item.current_value = payload.current_value
    if payload.status:
        item.status = payload.status.value
    item.updated_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _to_dict(item)


@router.delete("/{goal_id}", response_model=dict)
async def delete_goal(goal_id: str, current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    item = (await session.execute(select(StrategicGoal).where(StrategicGoal.id == goal_id, StrategicGoal.school_id == school_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Goal not found")
    await session.delete(item)
    await session.commit()
    return {"success": True, "message": "Goal deleted"}
