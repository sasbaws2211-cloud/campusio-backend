"""School-level compliance / accreditation register — see models/compliance.py."""
from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_roles
from database import get_session
from models.compliance import (
    ComplianceItem, ComplianceItemCreate, ComplianceItemUpdate, ComplianceReviewRequest, ComplianceStatus,
)
from models.user import User, UserRole

router = APIRouter(prefix="/compliance", tags=["Compliance & Accreditation"])

STAFF_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR)


def _school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


def _to_dict(item: ComplianceItem) -> dict:
    return {
        "id": item.id, "name": item.name, "category": item.category, "issuing_body": item.issuing_body,
        "requirement_description": item.requirement_description, "due_date": item.due_date, "status": item.status,
        "renewal_frequency_months": item.renewal_frequency_months, "last_reviewed_at": item.last_reviewed_at,
        "reviewed_by": item.reviewed_by, "notes": item.notes, "created_at": item.created_at,
    }


@router.post("", response_model=dict)
async def create_item(payload: ComplianceItemCreate, current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    item = ComplianceItem(school_id=school_id, created_by=current_user.id, **{**payload.model_dump(), "category": payload.category.value})
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _to_dict(item)


@router.get("", response_model=List[dict])
async def list_items(
    status_filter: Optional[str] = None, category: Optional[str] = None,
    current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    stmt = select(ComplianceItem).where(ComplianceItem.school_id == school_id)
    if status_filter:
        stmt = stmt.where(ComplianceItem.status == status_filter)
    if category:
        stmt = stmt.where(ComplianceItem.category == category)
    result = await session.execute(stmt.order_by(ComplianceItem.due_date))
    return [_to_dict(i) for i in result.scalars().all()]


@router.get("/summary", response_model=dict)
async def summary(current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    items = (await session.execute(select(ComplianceItem).where(ComplianceItem.school_id == school_id))).scalars().all()
    today = date.today().isoformat()
    upcoming_30 = (date.today().toordinal())
    from datetime import timedelta
    horizon = (date.today() + timedelta(days=30)).isoformat()

    by_status = {}
    for i in items:
        by_status[i.status] = by_status.get(i.status, 0) + 1

    due_soon = [
        {"id": i.id, "name": i.name, "due_date": i.due_date, "status": i.status}
        for i in items if i.due_date and today <= i.due_date <= horizon and i.status not in (ComplianceStatus.COMPLIANT.value,)
    ]
    overdue = [
        {"id": i.id, "name": i.name, "due_date": i.due_date, "status": i.status}
        for i in items if i.due_date and i.due_date < today and i.status not in (ComplianceStatus.COMPLIANT.value,)
    ]
    return {"total": len(items), "by_status": by_status, "due_within_30_days": due_soon, "overdue": overdue}


@router.put("/{item_id}", response_model=dict)
async def update_item(item_id: str, payload: ComplianceItemUpdate, current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    item = (await session.execute(select(ComplianceItem).where(ComplianceItem.id == item_id, ComplianceItem.school_id == school_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Compliance item not found")
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


@router.post("/{item_id}/review", response_model=dict)
async def review_item(item_id: str, payload: ComplianceReviewRequest, current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    item = (await session.execute(select(ComplianceItem).where(ComplianceItem.id == item_id, ComplianceItem.school_id == school_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Compliance item not found")
    item.status = payload.status.value
    item.last_reviewed_at = datetime.utcnow()
    item.reviewed_by = current_user.id
    if payload.notes:
        item.notes = payload.notes
    item.updated_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _to_dict(item)


@router.delete("/{item_id}", response_model=dict)
async def delete_item(item_id: str, current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    item = (await session.execute(select(ComplianceItem).where(ComplianceItem.id == item_id, ComplianceItem.school_id == school_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Compliance item not found")
    await session.delete(item)
    await session.commit()
    return {"success": True, "message": "Compliance item deleted"}
