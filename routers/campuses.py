"""Campus Management Router (light multi-campus support)"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from sqlmodel import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
from typing import List, Optional

from models.campus import Campus, CampusCreate, CampusUpdate
from models.user import User, UserRole
from database import get_session
from auth import require_roles
from dependencies import assert_campus_access

router = APIRouter(prefix="/campuses", tags=["Campuses"])

READ_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR, UserRole.REGISTRAR)
WRITE_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)
# Creating/deleting a whole campus is a school-level decision — a campus-scoped
# admin (User.campus_id set) manages their one campus's day-to-day data but
# doesn't get to add or remove campuses from the school.
CREATE_DELETE_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)


@router.get("", response_model=List[dict])
async def list_campuses(
    school_id: Optional[str] = None,
    current_user: User = Depends(require_roles(*READ_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    # A super admin isn't tied to one school, so they pick which school's
    # campuses to see (e.g. the account-creation form on the super admin
    # portal, when assigning a new school admin to a campus). Every other
    # caller is locked to their own school regardless of what's passed.
    if current_user.role != UserRole.SUPER_ADMIN or not school_id:
        school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(Campus).where(Campus.school_id == school_id)
    if current_user.campus_id:
        query = query.where(Campus.id == current_user.campus_id)
    result = await session.execute(query.order_by(Campus.name))
    return [jsonable_encoder(c) for c in result.scalars().all()]


@router.post("", response_model=dict)
async def create_campus(
    data: CampusCreate,
    current_user: User = Depends(require_roles(*CREATE_DELETE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    if current_user.campus_id:
        raise HTTPException(status_code=403, detail="Only a school-wide admin can create campuses")

    campus = Campus(**data.dict(), school_id=school_id)
    session.add(campus)
    await session.commit()
    await session.refresh(campus)
    return jsonable_encoder(campus)


@router.get("/{campus_id}", response_model=dict)
async def get_campus(
    campus_id: str,
    current_user: User = Depends(require_roles(*READ_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(Campus).where(and_(Campus.id == campus_id, Campus.school_id == school_id))
    )
    campus = result.scalar_one_or_none()
    if not campus:
        raise HTTPException(status_code=404, detail="Campus not found")
    assert_campus_access(current_user, campus.id)
    return jsonable_encoder(campus)


@router.put("/{campus_id}", response_model=dict)
async def update_campus(
    campus_id: str,
    data: CampusUpdate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(Campus).where(and_(Campus.id == campus_id, Campus.school_id == school_id))
    )
    campus = result.scalar_one_or_none()
    if not campus:
        raise HTTPException(status_code=404, detail="Campus not found")
    assert_campus_access(current_user, campus.id)

    update_data = data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(campus, key, value)
    campus.updated_at = datetime.utcnow()

    session.add(campus)
    await session.commit()
    await session.refresh(campus)
    return jsonable_encoder(campus)


@router.delete("/{campus_id}", response_model=dict)
async def delete_campus(
    campus_id: str,
    current_user: User = Depends(require_roles(*CREATE_DELETE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    if current_user.campus_id:
        raise HTTPException(status_code=403, detail="Only a school-wide admin can delete campuses")

    result = await session.execute(
        select(Campus).where(and_(Campus.id == campus_id, Campus.school_id == school_id))
    )
    campus = result.scalar_one_or_none()
    if not campus:
        raise HTTPException(status_code=404, detail="Campus not found")

    await session.delete(campus)
    await session.commit()
    return {"success": True, "message": "Campus deleted"}
