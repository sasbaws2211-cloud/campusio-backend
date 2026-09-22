"""Roles & Permissions admin router — the school-admin-facing surface for
the fine-grained RBAC layer (models/rbac.py, services/permission_service.py).

Managing roles is itself gated by the existing coarse `require_roles`, not
`require_permission` — composing permission sets is an admin bootstrap
concern, not something to gate behind the very permissions being composed.

Scoping: SUPER_ADMIN manages system roles (school_id=None), visible to and
read-only for every school. SCHOOL_ADMIN manages custom roles scoped to
their own school_id — they can list system roles (to clone from) but never
edit or delete them.
"""
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from auth import get_redis, require_roles
from database import get_session
from models.rbac import (
    Permission,
    PermissionResponse,
    Role,
    RoleAssignRequest,
    RoleCloneRequest,
    RoleCreate,
    RoleResponse,
    RolePermission,
    RoleUpdate,
)
from models.user import User, UserRole
from services.permission_service import invalidate_role_permissions_cache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/roles", tags=["Roles & Permissions"])

ROLE_ADMIN_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)


async def _permission_codes_for_role(session: AsyncSession, role_id: str) -> List[str]:
    result = await session.execute(
        select(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id == role_id)
    )
    return sorted(row[0] for row in result.all())


def _role_to_response(role: Role, codes: List[str]) -> RoleResponse:
    return RoleResponse(
        id=role.id,
        school_id=role.school_id,
        name=role.name,
        is_system=role.is_system,
        permission_codes=codes,
        created_at=role.created_at,
        updated_at=role.updated_at,
    )


def _assert_editable(role: Role, current_user: User) -> None:
    """Only a school's own custom roles can be edited/deleted, and never a
    system role — SUPER_ADMIN included, since system roles are the shared
    default every school falls back to."""
    if role.is_system:
        raise HTTPException(status_code=403, detail="System roles cannot be modified. Clone it into a custom role instead.")
    if current_user.role != UserRole.SUPER_ADMIN and role.school_id != current_user.school_id:
        raise HTTPException(status_code=403, detail="You can only manage your own school's roles")


@router.get("/permissions", response_model=List[PermissionResponse])
async def list_permission_catalog(
    current_user: User = Depends(require_roles(*ROLE_ADMIN_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """The full permission catalog, for a checkbox UI grouped by `module`."""
    result = await session.execute(select(Permission).order_by(Permission.module, Permission.code))
    return [PermissionResponse.model_validate(p) for p in result.scalars().all()]


@router.get("", response_model=List[RoleResponse])
async def list_roles(
    current_user: User = Depends(require_roles(*ROLE_ADMIN_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """System roles (visible to everyone, cloneable) plus this school's own
    custom roles. SUPER_ADMIN with no school_id sees system roles only."""
    # For SUPER_ADMIN with no school_id, school_id == None matches nothing
    # but system rows (school_id is already None for those), so this
    # collapses to "system roles only" for them — matching the docstring.
    query = select(Role).where((Role.is_system == True) | (Role.school_id == current_user.school_id))  # noqa: E712
    result = await session.execute(query.order_by(Role.is_system.desc(), Role.name))
    roles = result.scalars().all()
    return [_role_to_response(r, await _permission_codes_for_role(session, r.id)) for r in roles]


@router.post("", response_model=RoleResponse, status_code=status.HTTP_201_CREATED)
async def create_role(
    role_data: RoleCreate,
    current_user: User = Depends(require_roles(*ROLE_ADMIN_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """Compose a new custom role for this school from the permission catalog."""
    if not current_user.school_id:
        raise HTTPException(status_code=400, detail="No school context — custom roles must belong to a school")

    valid_codes = set(
        (await session.execute(select(Permission.code).where(Permission.code.in_(role_data.permission_codes)))).scalars().all()
    )
    unknown = set(role_data.permission_codes) - valid_codes
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown permission codes: {sorted(unknown)}")

    role = Role(school_id=current_user.school_id, name=role_data.name, is_system=False, created_by=current_user.id)
    session.add(role)
    await session.flush()

    permission_ids = (
        await session.execute(select(Permission.id).where(Permission.code.in_(valid_codes)))
    ).scalars().all()
    for permission_id in permission_ids:
        session.add(RolePermission(role_id=role.id, permission_id=permission_id))

    await session.commit()
    return _role_to_response(role, sorted(valid_codes))


@router.post("/clone/{role_id}", response_model=RoleResponse, status_code=status.HTTP_201_CREATED)
async def clone_role(
    role_id: str,
    clone_data: RoleCloneRequest,
    current_user: User = Depends(require_roles(*ROLE_ADMIN_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """Clone any role visible to this school (a system role or one of the
    school's own) into a new, independently-editable custom role."""
    if not current_user.school_id:
        raise HTTPException(status_code=400, detail="No school context — custom roles must belong to a school")

    source = (await session.execute(select(Role).where(Role.id == role_id))).scalar_one_or_none()
    if source is None:
        raise HTTPException(status_code=404, detail="Role not found")
    if not source.is_system and current_user.role != UserRole.SUPER_ADMIN and source.school_id != current_user.school_id:
        raise HTTPException(status_code=403, detail="You can only clone system roles or your own school's roles")

    codes = await _permission_codes_for_role(session, source.id)
    new_role = Role(
        school_id=current_user.school_id,
        name=clone_data.name or f"{source.name} (Copy)",
        is_system=False,
        created_by=current_user.id,
    )
    session.add(new_role)
    await session.flush()

    permission_ids = (
        await session.execute(select(Permission.id).where(Permission.code.in_(codes)))
    ).scalars().all()
    for permission_id in permission_ids:
        session.add(RolePermission(role_id=new_role.id, permission_id=permission_id))

    await session.commit()
    return _role_to_response(new_role, codes)


@router.put("/{role_id}", response_model=RoleResponse)
async def update_role(
    role_id: str,
    update_data: RoleUpdate,
    current_user: User = Depends(require_roles(*ROLE_ADMIN_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """Rename a custom role and/or replace its full set of granted permissions."""
    role = (await session.execute(select(Role).where(Role.id == role_id))).scalar_one_or_none()
    if role is None:
        raise HTTPException(status_code=404, detail="Role not found")
    _assert_editable(role, current_user)

    if update_data.name is not None:
        role.name = update_data.name

    if update_data.permission_codes is not None:
        valid_codes = set(
            (await session.execute(select(Permission.code).where(Permission.code.in_(update_data.permission_codes)))).scalars().all()
        )
        unknown = set(update_data.permission_codes) - valid_codes
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown permission codes: {sorted(unknown)}")

        await session.execute(
            RolePermission.__table__.delete().where(RolePermission.role_id == role.id)
        )
        permission_ids = (
            await session.execute(select(Permission.id).where(Permission.code.in_(valid_codes)))
        ).scalars().all()
        for permission_id in permission_ids:
            session.add(RolePermission(role_id=role.id, permission_id=permission_id))

    role.updated_at = datetime.utcnow()
    session.add(role)
    await session.commit()
    await invalidate_role_permissions_cache(role.id)

    codes = await _permission_codes_for_role(session, role.id)
    return _role_to_response(role, codes)


@router.delete("/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(
    role_id: str,
    current_user: User = Depends(require_roles(*ROLE_ADMIN_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """Delete a custom role. Blocked while any user is still assigned to it."""
    role = (await session.execute(select(Role).where(Role.id == role_id))).scalar_one_or_none()
    if role is None:
        raise HTTPException(status_code=404, detail="Role not found")
    _assert_editable(role, current_user)

    assignee_count = (
        await session.execute(select(User.id).where(User.role_id == role.id))
    ).scalars().first()
    if assignee_count is not None:
        raise HTTPException(status_code=400, detail="Cannot delete a role that is still assigned to users. Reassign them first.")

    await session.execute(RolePermission.__table__.delete().where(RolePermission.role_id == role.id))
    await session.delete(role)
    await session.commit()
    await invalidate_role_permissions_cache(role.id)
    return None


@router.post("/assign/{user_id}", response_model=dict)
async def assign_role_to_user(
    user_id: str,
    assign_data: RoleAssignRequest,
    current_user: User = Depends(require_roles(*ROLE_ADMIN_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """Assign a custom role to a staff member, or clear it (role_id=None)
    to fall back to the system role matching their `role` enum. The
    target's `role` enum is never changed — only which Role governs their
    fine-grained permissions."""
    role_id = assign_data.role_id
    target = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if current_user.role != UserRole.SUPER_ADMIN and target.school_id != current_user.school_id:
        raise HTTPException(status_code=403, detail="You can only manage your own school's users")

    if role_id is not None:
        role = (await session.execute(select(Role).where(Role.id == role_id))).scalar_one_or_none()
        if role is None:
            raise HTTPException(status_code=404, detail="Role not found")
        if not role.is_system and role.school_id != target.school_id:
            raise HTTPException(status_code=400, detail="Role does not belong to this user's school")

    target.role_id = role_id
    session.add(target)
    await session.commit()

    # Bust the cached user object (auth.py) so the new role_id takes effect
    # on this user's very next request instead of waiting out the 15-min TTL.
    redis_client = await get_redis()
    if redis_client:
        try:
            await redis_client.delete(f"user:{user_id}")
        except Exception as e:
            logger.warning(f"Redis cache invalidation error (user): {e}")

    return {"user_id": user_id, "role_id": role_id}
