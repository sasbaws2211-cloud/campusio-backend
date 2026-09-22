"""User models for authentication and RBAC"""
from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime
from enum import Enum
import uuid


class UserRole(str, Enum):
    SUPER_ADMIN = "super_admin"
    SCHOOL_ADMIN = "school_admin"
    HR = "hr"
    TEACHER = "teacher"
    STUDENT = "student"
    PARENT = "parent"
    SECURITY_OFFICER = "security_officer"
    DRIVER = "driver"
    NURSE = "nurse"
    REGISTRAR = "registrar"
    STOREKEEPER = "storekeeper"
    CANTEEN_STAFF = "canteen_staff"
    COUNSELOR = "counselor"
    SEN_COORDINATOR = "sen_coordinator"
    SAFEGUARDING_LEAD = "safeguarding_lead"


class UserBase(SQLModel):
    email: str = Field(index=True, unique=True)
    first_name: str
    last_name: str
    phone: Optional[str] = None
    role: UserRole
    school_id: Optional[str] = Field(default=None, index=True)
    # When set, this user is restricted to this one campus (see dependencies.py's
    # resolve_campus_scope/resolve_write_campus_id/assert_campus_access) — a real
    # tenancy boundary layered on top of Campus, which by itself is just a label.
    # None means unrestricted (full school access), same as every user today.
    campus_id: Optional[str] = Field(default=None, index=True)
    is_active: bool = True
    # Opt-in fine-grained permissions (see models/rbac.py). None (the default
    # for every user today) means "use the system role matching `role`" — no
    # backfill needed. Set only when a school assigns this user a custom
    # role composed in the Roles & Permissions admin screen. `role` itself
    # is untouched either way and keeps driving portal routing and every
    # existing require_roles() check.
    role_id: Optional[str] = Field(default=None, index=True)


class User(UserBase, table=True):
    __tablename__ = "users"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    password_hash: str
    # Fernet-encrypted (services/ai_key_crypto.py), never raw — only for
    # portal accounts, so an admin can look up a not-yet-first-logged-in
    # user's generated password to hand it over. Cleared on first login
    # (see routers/auth.py:change_password). Use auth.py's
    # encrypt_onboarding_password/decrypt_onboarding_password to read/write.
    plain_text_password: Optional[str] = None
    must_change_password: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    last_login: Optional[datetime] = None
    # Bumped on password change / explicit "log out everywhere" — any JWT
    # issued (iat) before this timestamp is rejected in get_current_user,
    # even if it hasn't expired yet. None means no forced-logout has ever
    # happened, so every token issued so far is still honored.
    sessions_valid_after: Optional[datetime] = None


class UserCreate(SQLModel):
    email: str
    password: str
    first_name: str
    last_name: str
    phone: Optional[str] = None
    role: UserRole
    school_id: Optional[str] = None
    campus_id: Optional[str] = None


class UserLogin(SQLModel):
    email: str
    password: str


class UserResponse(SQLModel):
    id: str
    email: str
    first_name: str
    last_name: str
    phone: Optional[str]
    role: UserRole
    school_id: Optional[str]
    campus_id: Optional[str] = None
    is_active: bool
    must_change_password: bool = False
    created_at: datetime
    last_login: Optional[datetime]
