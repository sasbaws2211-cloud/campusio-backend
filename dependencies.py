"""
Shared dependencies for all routers
Centralizes authentication, database, and user context injection
"""
import logging
from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from auth import get_current_user
from database import get_session
from models.user import User

logger = logging.getLogger(__name__)

# Alias get_session as get_db for consistency with codebase
get_db = get_session


async def get_current_school_id(current_user: User = Depends(get_current_user)) -> str:
    """Extract school_id from current user
    
    Used to automatically scope all queries to the user's school.
    SUPER_ADMIN can access any school (school_id will be from user selection).
    Regular users can only access their assigned school.
    
    Args:
        current_user: Current authenticated user
        
    Returns:
        School ID string
        
    Raises:
        HTTPException 403: If user has no school access
    """
    if not current_user.school_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not associated with any school"
        )
    return str(current_user.school_id)


def resolve_campus_scope(current_user: User, requested_campus_id: str | None = None) -> str | None:
    """The campus_id a read query should be filtered by.

    A campus-scoped user (User.campus_id set) always sees only their own
    campus — their own assignment wins over whatever campus_id a query param
    asks for, so a scoped admin can never widen their view by just passing a
    different id. An unscoped user (campus_id is None, e.g. most
    SCHOOL_ADMIN/SUPER_ADMIN accounts today) gets whatever was requested,
    including None for "all campuses" — unchanged from current behavior.
    """
    if current_user.campus_id:
        return current_user.campus_id
    return requested_campus_id


def resolve_write_campus_id(current_user: User, requested_campus_id: str | None = None) -> str | None:
    """The campus_id to stamp on a newly created record.

    Same precedence as resolve_campus_scope: a campus-scoped user's own
    campus always wins, so they can't create a record in a campus other than
    their own no matter what the request body says. An unscoped user's
    submitted value (possibly None, i.e. school-wide) passes through.
    """
    if current_user.campus_id:
        return current_user.campus_id
    return requested_campus_id


def assert_campus_access(current_user: User, record_campus_id: str | None) -> None:
    """Raise 403 if a campus-scoped user is trying to read/write a record
    outside their assigned campus. Records with no campus_id (school-wide,
    or predating campus assignment) are also off-limits to a scoped user —
    widening a record's scope is a school-level decision, not theirs to
    make by editing it."""
    if current_user.campus_id and record_campus_id != current_user.campus_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only access records in your assigned campus"
        )


# Re-export commonly used dependencies for convenience
__all__ = [
    "get_db",
    "get_session",
    "get_current_user",
    "get_current_school_id",
    "resolve_campus_scope",
    "resolve_write_campus_id",
    "assert_campus_access",
]
