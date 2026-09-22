"""Fine-grained, composable RBAC: permission catalog and roles built from it.

This is additive to the existing UserRole enum (models/user.py), not a
replacement. Every User keeps its `role` enum column, which continues to
drive portal routing and the existing `require_roles` dependency unchanged.
`User.role_id` (nullable) is the opt-in layer: when unset, a user's
permissions resolve from the system Role that mirrors their `role` enum;
when set (a school assigned them a custom role), permissions resolve from
that Role's granted Permissions instead. See services/permission_service.py
for the resolution logic and auth.py's `require_permission` for enforcement.
"""
from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime
import uuid


class Permission(SQLModel, table=True):
    """One granted-or-not action in the permission catalog.

    Codes follow `<module>.<resource>.<action>` (e.g. "finance.journal.post")
    with a small, reused action vocabulary (view/create/update/delete/
    approve/post/reverse/manage) so the catalog stays small enough for an
    admin to compose a role against in a checkbox UI, rather than one code
    per API endpoint.
    """
    __tablename__ = "permissions"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    code: str = Field(index=True, unique=True)
    module: str = Field(index=True)
    description: str


class Role(SQLModel, table=True):
    """A named, composable set of permissions.

    school_id is None for system roles (one per UserRole enum value, seeded
    by scripts/seed_permissions.py, visible to every school) and set for a
    school's own custom role. is_system roles cannot be edited or deleted
    through routers/roles.py — only cloned into a new custom role.
    """
    __tablename__ = "roles"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: Optional[str] = Field(default=None, index=True)
    name: str
    is_system: bool = Field(default=False, index=True)
    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class RolePermission(SQLModel, table=True):
    """Many-to-many grant: this role includes this permission."""
    __tablename__ = "role_permissions"

    role_id: str = Field(primary_key=True, foreign_key="roles.id")
    permission_id: str = Field(primary_key=True, foreign_key="permissions.id")


class PermissionResponse(SQLModel):
    id: str
    code: str
    module: str
    description: str


class RoleCreate(SQLModel):
    name: str
    permission_codes: list[str] = []


class RoleUpdate(SQLModel):
    name: Optional[str] = None
    permission_codes: Optional[list[str]] = None


class RoleCloneRequest(SQLModel):
    name: Optional[str] = None


class RoleAssignRequest(SQLModel):
    role_id: Optional[str] = None


class RoleResponse(SQLModel):
    id: str
    school_id: Optional[str]
    name: str
    is_system: bool
    permission_codes: list[str]
    created_at: datetime
    updated_at: datetime
