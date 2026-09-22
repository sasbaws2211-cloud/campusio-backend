"""Permission resolution for the fine-grained RBAC layer (models/rbac.py).

Additive to the existing UserRole/require_roles mechanism in auth.py — see
models/rbac.py's module docstring for how the two coexist. A user's
permissions come from their assigned custom Role (`user.role_id`) if set,
else from the system Role that mirrors their `role` enum — so an unassigned
user (every user today) resolves to exactly what a school's default setup
grants, with zero backfill required.
"""
import json
import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from auth import get_redis
from models.rbac import Permission, Role, RolePermission
from models.user import User

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 900  # matches auth.py's user cache TTL


async def _resolve_role_id(session: AsyncSession, user: User) -> Optional[str]:
    """The Role row that governs `user`'s permissions."""
    if user.role_id:
        return user.role_id
    result = await session.execute(
        select(Role.id).where(
            Role.is_system == True,  # noqa: E712
            Role.school_id.is_(None),
            Role.name == user.role.value,
        )
    )
    return result.scalar_one_or_none()


async def get_user_permissions(session: AsyncSession, user: User) -> set[str]:
    """All permission codes granted to `user`, cached per-role for 15 minutes.

    Returns an empty set (not an error) if the user's role has no matching
    system/custom Role row yet — e.g. before scripts/seed_permissions.py has
    run for a given school. Endpoints gated by require_roles are unaffected
    either way; only require_permission()-gated endpoints consult this.
    """
    role_id = await _resolve_role_id(session, user)
    if role_id is None:
        return set()

    redis_client = await get_redis()
    cache_key = f"role_permissions:{role_id}"

    if redis_client:
        try:
            cached = await redis_client.get(cache_key)
            if cached is not None:
                return set(json.loads(cached))
        except Exception as e:
            logger.warning(f"Redis cache read error (permissions): {e}")

    result = await session.execute(
        select(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id == role_id)
    )
    codes = {row[0] for row in result.all()}

    if redis_client:
        try:
            await redis_client.setex(cache_key, CACHE_TTL_SECONDS, json.dumps(list(codes)))
        except Exception as e:
            logger.warning(f"Redis cache write error (permissions): {e}")

    return codes


async def invalidate_role_permissions_cache(role_id: str) -> None:
    """Call after changing a role's permission grants so callers see the
    change immediately instead of waiting out the 15-minute TTL."""
    redis_client = await get_redis()
    if redis_client:
        try:
            await redis_client.delete(f"role_permissions:{role_id}")
        except Exception as e:
            logger.warning(f"Redis cache invalidation error (permissions): {e}")
