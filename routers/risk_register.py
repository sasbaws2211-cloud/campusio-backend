"""Enterprise risk register — see models/risk_register.py."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_roles
from database import get_session
from models.risk_register import RiskRegisterItem, RiskRegisterItemCreate, RiskRegisterItemUpdate
from models.user import User, UserRole

router = APIRouter(prefix="/risk-register", tags=["Risk Register"])

STAFF_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR)


def _school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


def _to_dict(item: RiskRegisterItem) -> dict:
    return {
        "id": item.id, "title": item.title, "category": item.category, "description": item.description,
        "likelihood": item.likelihood, "impact": item.impact, "risk_score": item.likelihood * item.impact,
        "mitigation_plan": item.mitigation_plan, "owner_id": item.owner_id, "status": item.status,
        "review_date": item.review_date, "created_at": item.created_at,
    }


@router.post("", response_model=dict)
async def create_risk(payload: RiskRegisterItemCreate, current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    item = RiskRegisterItem(school_id=school_id, created_by=current_user.id, **{**payload.model_dump(), "category": payload.category.value})
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _to_dict(item)


@router.get("", response_model=List[dict])
async def list_risks(
    status_filter: Optional[str] = None, category: Optional[str] = None,
    current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    stmt = select(RiskRegisterItem).where(RiskRegisterItem.school_id == school_id)
    if status_filter:
        stmt = stmt.where(RiskRegisterItem.status == status_filter)
    if category:
        stmt = stmt.where(RiskRegisterItem.category == category)
    result = await session.execute(stmt)
    items = [_to_dict(r) for r in result.scalars().all()]
    items.sort(key=lambda r: -r["risk_score"])
    return items


@router.get("/summary", response_model=dict)
async def risk_summary(current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    items = (await session.execute(select(RiskRegisterItem).where(RiskRegisterItem.school_id == school_id, RiskRegisterItem.status != "closed"))).scalars().all()
    high_risk = [i for i in items if i.likelihood * i.impact >= 15]
    by_category = {}
    for i in items:
        by_category[i.category] = by_category.get(i.category, 0) + 1
    return {
        "total_open_risks": len(items),
        "high_risk_count": len(high_risk),
        "by_category": by_category,
        "high_risk_items": [_to_dict(i) for i in high_risk],
    }


@router.put("/{risk_id}", response_model=dict)
async def update_risk(risk_id: str, payload: RiskRegisterItemUpdate, current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    item = (await session.execute(select(RiskRegisterItem).where(RiskRegisterItem.id == risk_id, RiskRegisterItem.school_id == school_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Risk item not found")
    update_data = payload.model_dump(exclude_unset=True)
    for field in ("category", "status"):
        if field in update_data and update_data[field] is not None:
            update_data[field] = update_data[field].value
    for key, value in update_data.items():
        setattr(item, key, value)
    item.updated_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _to_dict(item)


@router.delete("/{risk_id}", response_model=dict)
async def delete_risk(risk_id: str, current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    item = (await session.execute(select(RiskRegisterItem).where(RiskRegisterItem.id == risk_id, RiskRegisterItem.school_id == school_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Risk item not found")
    await session.delete(item)
    await session.commit()
    return {"success": True, "message": "Risk item deleted"}
