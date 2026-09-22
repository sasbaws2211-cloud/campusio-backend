"""Authentication router with integrated OTP support"""
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Request
from sqlmodel import select, SQLModel
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta
from typing import Optional
import csv
import io
import secrets
import string
from models.user import User, UserCreate, UserLogin, UserRole
from services.audit_service import log_event


class ChangePasswordRequest(SQLModel):
    new_password: str


class BootstrapSuperAdminRequest(SQLModel):
    email: str
    password: str
    first_name: str
    last_name: str
    phone: Optional[str] = None


class CreateAdminRequest(SQLModel):
    email: str
    password: str
    first_name: str
    last_name: str
    phone: Optional[str] = None
from models.otp import (
    OTPVerificationRequest, OTPVerificationResponse, OTPSettings,
    OTPAdminSettings, OTPAdminSettingsRequest, OTPAdminSettingsResponse,
    OTPSettingsUpdateRequest, OTPResendRequest
)
from database import get_session
from auth import (
    get_password_hash, verify_password, create_access_token,
    get_current_user, require_roles, validate_password_strength,
    encrypt_onboarding_password, check_login_allowed, check_login_rate_limit,
    record_login_attempt, revoke_token, security,
)
from jose import jwt as jose_jwt
from config import get_settings
from utils.otp import (
    create_otp, verify_otp, send_otp_email, send_otp_sms,
    get_otp_settings, create_or_update_otp_settings,
    get_admin_otp_settings, create_or_update_admin_otp_settings
)

router = APIRouter(prefix="/auth", tags=["Authentication"])


class UpdateUserStatusRequest(SQLModel):
    is_active: bool


# Roles that require mandatory OTP
MANDATORY_OTP_ROLES = {UserRole.SCHOOL_ADMIN, UserRole.HR, UserRole.SUPER_ADMIN}


def _generate_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%"
    return ''.join(secrets.choice(alphabet) for _ in range(length))


@router.post("/register", response_model=dict)
async def register(
    user_data: UserCreate,
    request: Request,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Create a new user account (admin only). Public self-registration is not permitted."""
    if user_data.role == UserRole.SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot create super admin accounts via this endpoint"
        )

    if current_user.role == UserRole.SCHOOL_ADMIN:
        if user_data.role == UserRole.SCHOOL_ADMIN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="School admins cannot create other school admin accounts"
            )
        if user_data.school_id and user_data.school_id != current_user.school_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only create users for your own school"
            )
        user_data.school_id = current_user.school_id

    # A campus-scoped admin (User.campus_id set) can only create accounts
    # within their own campus — their assignment always wins, mirroring
    # dependencies.py::resolve_write_campus_id used elsewhere for the same
    # reason. An unscoped admin's chosen campus_id (including None, i.e.
    # school-wide) passes through.
    if current_user.campus_id:
        user_data.campus_id = current_user.campus_id

    result = await session.execute(select(User).where(User.email == user_data.email))
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )

    if user_data.password:
        validate_password_strength(user_data.password)
    plain_password = user_data.password or _generate_password()

    user = User(
        email=user_data.email,
        password_hash=get_password_hash(plain_password),
        plain_text_password=encrypt_onboarding_password(plain_password),
        first_name=user_data.first_name,
        last_name=user_data.last_name,
        phone=user_data.phone,
        role=user_data.role,
        school_id=user_data.school_id,
        campus_id=user_data.campus_id,
        must_change_password=True,
    )

    session.add(user)
    await session.commit()
    await session.refresh(user)

    is_mandatory = user.role in MANDATORY_OTP_ROLES
    await create_or_update_otp_settings(
        session=session,
        user_id=user.id,
        is_enabled=is_mandatory,
        method="sms"
    )

    await log_event(
        session, actor=current_user, action="user.created", entity_type="user",
        entity_id=user.id, school_id=user.school_id,
        summary=f"{current_user.email} created a {user.role.value if hasattr(user.role, 'value') else user.role} account: {user.email}",
        ip_address=request.client.host if request.client else None,
    )

    return {
        "user": {
            "id": user.id,
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "phone": user.phone,
            "role": user.role,
            "school_id": user.school_id,
            "campus_id": user.campus_id,
            "is_active": user.is_active,
            "must_change_password": user.must_change_password,
            "created_at": user.created_at.isoformat(),
        },
        "generated_password": plain_password,
        "message": "Account created. Share these credentials with the user — they must change their password on first login."
    }


@router.post("/register/bulk", response_model=dict)
async def bulk_register(
    file: UploadFile = File(...),
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """
    Bulk-create user accounts from a CSV file (admin only).

    Required columns: email, first_name, last_name, role
    Optional columns: phone, school_id
    """
    content = await file.read()
    try:
        reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    except Exception:
        raise HTTPException(status_code=400, detail="Unable to parse CSV file")

    required_cols = {"email", "first_name", "last_name", "role"}
    if not required_cols.issubset(set(reader.fieldnames or [])):
        raise HTTPException(
            status_code=400,
            detail=f"CSV must contain columns: {', '.join(sorted(required_cols))}"
        )

    created, skipped = [], []

    for i, row in enumerate(reader, start=2):
        email = (row.get("email") or "").strip().lower()
        first_name = (row.get("first_name") or "").strip()
        last_name = (row.get("last_name") or "").strip()
        role_val = (row.get("role") or "").strip().lower()
        phone = (row.get("phone") or "").strip() or None
        school_id = (row.get("school_id") or "").strip() or None

        if not email or not first_name or not last_name or not role_val:
            skipped.append({"row": i, "reason": "Missing required field", "email": email})
            continue

        try:
            role = UserRole(role_val)
        except ValueError:
            skipped.append({"row": i, "reason": f"Unknown role '{role_val}'", "email": email})
            continue

        if role == UserRole.SUPER_ADMIN:
            skipped.append({"row": i, "reason": "Cannot create super_admin via bulk import", "email": email})
            continue

        if current_user.role == UserRole.SCHOOL_ADMIN:
            if role == UserRole.SCHOOL_ADMIN:
                skipped.append({"row": i, "reason": "School admins cannot create other school_admin accounts", "email": email})
                continue
            school_id = current_user.school_id

        existing = await session.execute(select(User).where(User.email == email))
        if existing.scalar_one_or_none():
            skipped.append({"row": i, "reason": "Email already registered", "email": email})
            continue

        plain_password = _generate_password()
        user = User(
            email=email,
            password_hash=get_password_hash(plain_password),
            plain_text_password=encrypt_onboarding_password(plain_password),
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            role=role,
            school_id=school_id,
            must_change_password=True,
        )
        session.add(user)
        await session.flush()

        is_mandatory = role in MANDATORY_OTP_ROLES
        await create_or_update_otp_settings(session=session, user_id=user.id, is_enabled=is_mandatory, method="sms")

        created.append({"email": email, "name": f"{first_name} {last_name}", "role": role_val, "generated_password": plain_password})

    await session.commit()

    return {
        "created": len(created),
        "skipped": len(skipped),
        "accounts": created,
        "errors": skipped,
    }


@router.post("/login", response_model=dict)
async def login(credentials: UserLogin, request: Request, session: AsyncSession = Depends(get_session)):
    """
    Unified login endpoint with intelligent OTP handling.

    Step 1: Validates email and password
    - If OTP is required: Returns temporary token + sends OTP
    - If OTP not required: Returns access token directly
    """
    client_ip = request.client.host if request.client else None
    await check_login_rate_limit(client_ip)
    await check_login_allowed(session, credentials.email)

    result = await session.execute(select(User).where(User.email == credentials.email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(credentials.password, user.password_hash):
        await record_login_attempt(
            session, credentials.email, success=False,
            user_id=user.id if user else None, failure_reason="invalid_credentials", request=request,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password"
        )

    if not user.is_active:
        await record_login_attempt(
            session, credentials.email, success=False,
            user_id=user.id, failure_reason="account_disabled", request=request,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is disabled"
        )

    # Get OTP settings for user
    otp_settings = await get_otp_settings(session, user.id)
    
    # Auto-create OTP settings if they don't exist (safety net for existing users)
    if not otp_settings:
        print(f"[LOGIN] OTPSettings not found for user {user.email} - creating...")
        is_mandatory = user.role in MANDATORY_OTP_ROLES
        otp_settings = await create_or_update_otp_settings(
            session=session,
            user_id=user.id,
            is_enabled=is_mandatory,
            method="sms"
        )
        print(f"[LOGIN] OTPSettings created - enabled: {otp_settings.is_enabled}, method: {otp_settings.method}")
    
    otp_required = otp_settings and otp_settings.is_enabled
    
    print(f"[LOGIN] User: {user.email}, OTP required: {otp_required}")
    if otp_settings:
        print(f"[LOGIN] OTP settings - enabled: {otp_settings.is_enabled}, method: {otp_settings.method}, mandatory: {otp_settings.is_mandatory}")
    
    if otp_required:
        # Generate OTP and send it
        otp_code, otp_id = await create_otp(session, user.id)
        
        # Send OTP via email or SMS based on settings
        if otp_settings.method == "sms" and user.phone:
            print(f"[LOGIN] OTP method is SMS, sending to {user.phone}")
            await send_otp_sms(user.phone, otp_code)
        else:
            print(f"[LOGIN] OTP method is email, sending to {user.email}")
            send_otp_email(user.email, otp_code, f"{user.first_name} {user.last_name}")
        
        # Create temporary token valid for 10 minutes — expires_delta must be
        # passed explicitly; None here would fall through to
        # create_access_token's own default (access_token_expire_minutes,
        # currently 24h), which is far too long-lived for a pre-2FA token.
        temp_token = create_access_token(
            data={
                "sub": user.id,
                "type": "otp_pending",
                "otp_id": otp_id
            },
            expires_delta=timedelta(minutes=10)
        )

        return {
            "status": "otp_required",
            "temporary_token": temp_token,
            "otp_method": otp_settings.method,
            "message": f"OTP sent to {otp_settings.method}"
        }
    
    else:
        # No OTP required, proceed directly to login
        user.last_login = datetime.utcnow()
        session.add(user)
        await session.commit()
        await record_login_attempt(session, credentials.email, success=True, user_id=user.id, request=request)

        access_token = create_access_token(data={"sub": user.id})

        return {
            "status": "authenticated",
            "access_token": access_token,
            "token_type": "bearer",
            "user": {
                "id": user.id,
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "phone": user.phone,
                "role": user.role,
                "school_id": user.school_id,
                "campus_id": user.campus_id,
                "is_active": user.is_active,
                "must_change_password": user.must_change_password,
                "created_at": user.created_at.isoformat(),
                "last_login": user.last_login.isoformat() if user.last_login else None
            }
        }


@router.post("/verify-otp", response_model=dict)
async def verify_otp_code(
    verification: OTPVerificationRequest,
    request: Request,
    session: AsyncSession = Depends(get_session)
):
    """
    Step 2: Verify OTP code and get access token.
    """
    await check_login_rate_limit(request.client.host if request.client else None)
    await check_login_allowed(session, verification.email)

    # Get user
    result = await session.execute(select(User).where(User.email == verification.email))
    user = result.scalar_one_or_none()

    if not user:
        await record_login_attempt(session, verification.email, success=False, failure_reason="invalid_credentials", request=request)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email"
        )

    # Verify OTP
    try:
        await verify_otp(session, user.id, verification.otp_code)
    except HTTPException as e:
        await record_login_attempt(session, verification.email, success=False, user_id=user.id, failure_reason="invalid_otp", request=request)
        raise e

    # Update last login and create final access token
    user.last_login = datetime.utcnow()
    session.add(user)
    await session.commit()
    await record_login_attempt(session, verification.email, success=True, user_id=user.id, request=request)

    access_token = create_access_token(data={"sub": user.id})
    
    return {
        "status": "authenticated",
        "access_token": access_token,
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "phone": user.phone,
            "role": user.role,
            "school_id": user.school_id,
            "campus_id": user.campus_id,
            "is_active": user.is_active,
            "must_change_password": user.must_change_password,
            "created_at": user.created_at.isoformat(),
            "last_login": user.last_login.isoformat() if user.last_login else None
        }
    }


@router.post("/resend-otp", response_model=dict)
async def resend_otp(
    data: OTPResendRequest,
    session: AsyncSession = Depends(get_session)
):
    """Resend OTP to user"""
    result = await session.execute(select(User).where(User.email == data.email))
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    otp_settings = await get_otp_settings(session, user.id)
    
    # Auto-create OTP settings if missing (safety net)
    if not otp_settings:
        print(f"[RESEND-OTP] OTPSettings not found for user {user.email} - creating...")
        is_mandatory = user.role in MANDATORY_OTP_ROLES
        otp_settings = await create_or_update_otp_settings(
            session=session,
            user_id=user.id,
            is_enabled=is_mandatory,
            method="sms"
        )
    
    if not otp_settings or not otp_settings.is_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OTP not enabled for this user"
        )
    
    # Generate and send OTP
    otp_code, otp_id = await create_otp(session, user.id)
    
    if otp_settings.method == "sms" and user.phone:
        print(f"[RESEND-OTP] Sending SMS to {user.phone}")
        success = await send_otp_sms(user.phone, otp_code)
    else:
        print(f"[RESEND-OTP] Sending email to {user.email}")
        success = send_otp_email(user.email, otp_code, f"{user.first_name} {user.last_name}")
    
    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to send OTP via {otp_settings.method}"
        )
    
    return {
        "message": f"OTP resent to {otp_settings.method}",
        "otp_method": otp_settings.method
    }


@router.get("/me", response_model=dict)
async def get_me(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Get current user info"""
    from services.permission_service import get_user_permissions  # deferred: avoids import cycle
    from services.plan_gating import get_school_plan_tier, TIER_MODULES, MODULE_LABELS  # deferred: avoids import cycle

    permissions = await get_user_permissions(session, current_user)

    # Plan-tier feature entitlements (services/plan_gating.py) — additive to
    # `role`/`permissions`, purely for the frontend to show/hide lock icons
    # on gated nav items without guessing; the backend gate on each router
    # is still the actual enforcement, this is UI-only. SUPER_ADMIN and a
    # user with no school_id both bypass every gate server-side (see
    # require_plan_feature), so they're reported as having every module
    # unlocked here too, rather than the frontend needing its own copy of
    # that bypass rule.
    if current_user.role == UserRole.SUPER_ADMIN or not current_user.school_id:
        plan_tier = None
        plan_modules = sorted(MODULE_LABELS.keys())
    else:
        tier = await get_school_plan_tier(session, current_user.school_id)
        plan_tier = tier.value
        plan_modules = sorted(TIER_MODULES.get(tier, set()))

    return {
        "id": current_user.id,
        "email": current_user.email,
        "first_name": current_user.first_name,
        "last_name": current_user.last_name,
        "phone": current_user.phone,
        "role": current_user.role,
        "school_id": current_user.school_id,
        "campus_id": current_user.campus_id,
        "is_active": current_user.is_active,
        "must_change_password": current_user.must_change_password,
        "created_at": current_user.created_at.isoformat(),
        "last_login": current_user.last_login.isoformat() if current_user.last_login else None,
        # Fine-grained RBAC (models/rbac.py) — additive to `role`. Empty for
        # every user until scripts/seed_permissions.py has run for their
        # school; existing role-based UI gating is unaffected either way.
        "permissions": sorted(permissions),
        # Plan-tier feature entitlements (services/plan_gating.py) — see
        # comment above. plan_tier is null for SUPER_ADMIN/no-school users
        # (nothing to display), plan_modules always lists every module key
        # this user can actually reach.
        "plan_tier": plan_tier,
        "plan_modules": plan_modules,
    }


@router.post("/change-password", response_model=dict)
async def change_password(
    body: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Change password. Required on first login when must_change_password is True.

    Invalidates every OTHER token already issued for this user (see
    auth.py's sessions_valid_after check in get_current_user) — a password
    change is exactly the moment you want every other session logged out,
    e.g. after a suspected compromise. The token used to make THIS request
    would otherwise also be caught by that same cutoff, so a fresh
    access_token is issued in the response for the caller to switch to,
    rather than logging the person out of the account they just secured.
    The cache entry is cleared, not just the DB row, because
    get_current_user's cached-vs-fresh reasoning means it otherwise
    wouldn't see the new sessions_valid_after until the cached entry's own
    15-minute TTL happens to expire."""
    validate_password_strength(body.new_password)

    # current_user may be a cache-reconstructed, session-DETACHED object
    # (see auth.py's get_current_user: a Redis cache hit builds a fresh
    # User(**dict) that was never SELECTed in this session) — session.add()
    # on that always schedules an INSERT, not an UPDATE, and collides on
    # the primary key. Re-fetch the row so we're mutating something the
    # session actually knows is persistent.
    result = await session.execute(select(User).where(User.id == current_user.id))
    db_user = result.scalar_one()

    db_user.password_hash = get_password_hash(body.new_password)
    db_user.plain_text_password = None
    db_user.must_change_password = False
    db_user.sessions_valid_after = datetime.utcnow()
    db_user.updated_at = datetime.utcnow()
    session.add(db_user)
    await session.commit()

    from auth import get_redis
    redis_client = await get_redis()
    if redis_client:
        try:
            await redis_client.delete(f"user:{db_user.id}")
        except Exception:
            pass

    new_access_token = create_access_token(data={"sub": db_user.id})

    return {
        "message": "Password changed successfully. You've been logged out of any other active sessions.",
        "access_token": new_access_token,
        "token_type": "bearer",
    }


@router.post("/logout", response_model=dict)
async def logout(
    credentials=Depends(security),
    current_user: User = Depends(get_current_user),
):
    """Explicitly invalidates the token used to make this request, via the
    jti blacklist in auth.py (see revoke_token) — unlike change-password,
    this logs out only THIS session, not every session for the account."""
    try:
        payload = jose_jwt.decode(credentials.credentials, get_settings().secret_key, algorithms=[get_settings().algorithm])
    except Exception:
        return {"message": "Logged out"}

    jti = payload.get("jti")
    exp = payload.get("exp")
    if jti and exp:
        await revoke_token(jti, exp)

    return {"message": "Logged out"}


@router.post("/bootstrap-superadmin", response_model=dict)
async def bootstrap_superadmin(
    body: BootstrapSuperAdminRequest,
    request: Request,
    session: AsyncSession = Depends(get_session)
):
    """Create the very first super admin account. No authentication required
    to call this — there's nobody to authenticate as yet on a fresh deploy.

    Self-locking: this endpoint works exactly once, ever. As soon as any
    super_admin account exists in the database, every subsequent call 403s
    permanently. There is no way to re-open it short of deleting every
    super_admin row directly in the database.
    """
    existing = await session.execute(select(User).where(User.role == UserRole.SUPER_ADMIN))
    if existing.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A super admin already exists. This endpoint only works once, on a fresh deploy with no super admin yet."
        )

    email_taken = await session.execute(select(User).where(User.email == body.email))
    if email_taken.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already in use")

    validate_password_strength(body.password)

    user = User(
        email=body.email,
        password_hash=get_password_hash(body.password),
        first_name=body.first_name,
        last_name=body.last_name,
        phone=body.phone,
        role=UserRole.SUPER_ADMIN,
        school_id=None,
        must_change_password=False,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)

    await log_event(
        session, actor=user, action="user.bootstrap_superadmin", entity_type="user",
        entity_id=user.id, summary=f"First super admin account created via bootstrap endpoint: {user.email}",
        ip_address=request.client.host if request.client else None,
    )

    return {
        "message": "Super admin created. This endpoint is now permanently locked.",
        "id": user.id,
        "email": user.email,
    }


@router.post("/create-admin", response_model=dict)
async def create_admin(
    body: CreateAdminRequest,
    request: Request,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Create an additional super admin account. Super admin only — this is
    the authenticated counterpart to /bootstrap-superadmin, used any time
    after the first account exists."""
    email_taken = await session.execute(select(User).where(User.email == body.email))
    if email_taken.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already in use")

    validate_password_strength(body.password)

    user = User(
        email=body.email,
        password_hash=get_password_hash(body.password),
        first_name=body.first_name,
        last_name=body.last_name,
        phone=body.phone,
        role=UserRole.SUPER_ADMIN,
        school_id=None,
        must_change_password=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)

    await log_event(
        session, actor=current_user, action="user.created_admin", entity_type="user",
        entity_id=user.id, summary=f"{current_user.email} created a new super admin account: {user.email}",
        ip_address=request.client.host if request.client else None,
    )

    return {"message": "Super admin account created", "id": user.id, "email": user.email}


@router.delete("/users/{user_id}", response_model=dict)
async def delete_user(
    user_id: str,
    request: Request,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Remove a user account. Super admin only. This is a soft delete —
    the row and its history stay (other tables reference user_id as a plain
    string field with no DB-level cascade, so a hard delete would silently
    orphan records across the app). The account is deactivated and its email
    is released (prefixed) so the address can be reused for a new account."""
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="You cannot remove your own account")

    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    old_email = user.email
    user.is_active = False
    user.email = f"deleted-{int(datetime.utcnow().timestamp())}-{old_email}"
    user.updated_at = datetime.utcnow()
    session.add(user)
    await session.commit()

    await log_event(
        session, actor=current_user, action="user.deleted", entity_type="user",
        entity_id=user_id, school_id=user.school_id,
        summary=f"{current_user.email} removed account: {old_email} ({user.role.value if hasattr(user.role, 'value') else user.role})",
        old_values={"email": old_email, "is_active": True},
        ip_address=request.client.host if request.client else None,
    )

    return {"message": f"Account {old_email} removed"}


@router.get("/users", response_model=list[dict])
async def list_users(
    school_id: str = None,
    role: UserRole = None,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """List users (admin only)"""
    query = select(User)
    
    if current_user.role == UserRole.SCHOOL_ADMIN:
        query = query.where(User.school_id == current_user.school_id)
    elif school_id:
        query = query.where(User.school_id == school_id)
    
    if role:
        query = query.where(User.role == role)
    
    result = await session.execute(query)
    users = result.scalars().all()
    
    return [
        {
            "id": u.id,
            "email": u.email,
            "first_name": u.first_name,
            "last_name": u.last_name,
            "phone": u.phone,
            "role": u.role,
            "school_id": u.school_id,
            "campus_id": u.campus_id,
            "is_active": u.is_active,
            "must_change_password": u.must_change_password,
            "created_at": u.created_at.isoformat(),
            "last_login": u.last_login.isoformat() if u.last_login else None
        }
        for u in users
    ]


@router.put("/users/{user_id}/status", response_model=dict)
async def update_user_status(
    user_id: str,
    body: UpdateUserStatusRequest,
    request: Request,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    is_active = body.is_active
    """Enable/disable user account"""
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if current_user.role == UserRole.SCHOOL_ADMIN and user.school_id != current_user.school_id:
        raise HTTPException(status_code=403, detail="Access denied")

    was_active = user.is_active
    user.is_active = is_active
    user.updated_at = datetime.utcnow()
    session.add(user)
    await session.commit()

    await log_event(
        session, actor=current_user, action="user.status_changed", entity_type="user",
        entity_id=user_id, school_id=user.school_id,
        summary=f"{current_user.email} {'enabled' if is_active else 'disabled'} account: {user.email}",
        old_values={"is_active": was_active}, new_values={"is_active": is_active},
        ip_address=request.client.host if request.client else None,
    )

    return {"message": f"User {'enabled' if is_active else 'disabled'} successfully"}


# ==================== OTP SETTINGS ENDPOINTS ====================

@router.get("/otp-settings", response_model=dict)
async def get_otp_settings_endpoint(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get user's OTP settings"""
    otp_settings = await get_otp_settings(session, current_user.id)
    
    return {
        "is_enabled": otp_settings.is_enabled if otp_settings else False,
        "is_mandatory": otp_settings.is_mandatory if otp_settings else False,
        "method": otp_settings.method if otp_settings else "email",
        "user_id": current_user.id
    }


@router.post("/otp-settings/update", response_model=dict)
async def update_otp_settings(
    data: OTPSettingsUpdateRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Update user's OTP settings"""
    if current_user.role in MANDATORY_OTP_ROLES and not data.is_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"OTP is mandatory for {current_user.role} role"
        )

    if data.method not in ["email", "sms"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Method must be 'email' or 'sms'"
        )

    otp_settings = await create_or_update_otp_settings(
        session=session,
        user_id=current_user.id,
        is_enabled=data.is_enabled,
        method=data.method
    )
    
    return {
        "message": "OTP settings updated",
        "is_enabled": otp_settings.is_enabled,
        "method": otp_settings.method
    }


# ==================== ADMIN OTP SETTINGS ENDPOINTS ====================

@router.get("/admin/otp-settings", response_model=dict)
async def get_admin_otp_settings_endpoint(
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """
    Get OTP settings for the school (admin only)
    
    Only Super Admin and School Admin can access
    """
    # Get the school_id from current user
    # Super admin manages all schools, so school_id is None
    # School admin manages their specific school
    school_id = current_user.school_id if current_user.role == UserRole.SCHOOL_ADMIN else None
    
    settings = await get_admin_otp_settings(session, school_id)
    
    if not settings:
        # Return default settings if none exist
        return {
            "is_enabled": True,
            "expiry_minutes": 10,
            "max_attempts": 3,
            "default_method": "sms",
            "require_for_roles": ["school_admin", "hr"]
        }
    
    return {
        "is_enabled": settings.is_enabled,
        "expiry_minutes": settings.expiry_minutes,
        "max_attempts": settings.max_attempts,
        "default_method": settings.default_method,
        "require_for_roles": settings.require_for_roles.split(",")
    }


@router.put("/admin/otp-settings", response_model=dict)
async def update_admin_otp_settings(
    settings_request: OTPAdminSettingsRequest,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """
    Update OTP settings for the school (admin only)
    
    Only Super Admin and School Admin can update
    """
    # Get the school_id from current user
    # Super admin manages all schools, so school_id is None
    # School admin manages their specific school
    school_id = current_user.school_id if current_user.role == UserRole.SCHOOL_ADMIN else None
    
    try:
        settings = await create_or_update_admin_otp_settings(
            session=session,
            school_id=school_id,
            is_enabled=settings_request.is_enabled,
            expiry_minutes=settings_request.expiry_minutes,
            max_attempts=settings_request.max_attempts,
            default_method=settings_request.default_method,
            require_for_roles=settings_request.require_for_roles
        )
        
        return {
            "message": "OTP admin settings updated successfully",
            "is_enabled": settings.is_enabled,
            "expiry_minutes": settings.expiry_minutes,
            "max_attempts": settings.max_attempts,
            "default_method": settings.default_method,
            "require_for_roles": settings.require_for_roles.split(",")
        }
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
