"""Authentication utilities"""
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from passlib.context import CryptContext
from jose import JWTError, jwt
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as redis
from models.user import User, UserRole
from models.school import School
from database import get_session
from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()

# ==================== Account lockout ====================
# A rolling window, not a stored "locked_until" timestamp — the lockout
# self-resolves as old failures age out of FAILED_LOGIN_WINDOW_MINUTES,
# so there's nothing to clear on unlock and no separate lockout state to
# get out of sync with reality.
FAILED_LOGIN_WINDOW_MINUTES = 15
FAILED_LOGIN_MAX_ATTEMPTS = 5
LOGIN_RATE_LIMIT_PER_HOUR = 30  # per source IP — a coarser, complementary defense to the per-email lockout above

# 🚀 OPTIMIZATION: Redis cache for user authentication
# Reduces database hits from every request to only cache misses
_redis_client: Optional[redis.Redis] = None

async def get_redis() -> redis.Redis:
    """Get or create Redis client"""
    global _redis_client
    if _redis_client is None:
        try:
            # Use REDIS_URL from config (Render provides this)
            redis_url = settings.redis_url
            
            # Try to connect to Redis
            _redis_client = await redis.from_url(
                redis_url,
                encoding="utf8",
                decode_responses=True,
                socket_connect_timeout=5
            )
            await _redis_client.ping()
            logger.info("✓ Redis cache initialized successfully")
        except Exception as e:
            logger.warning(f"⚠ Redis cache unavailable ({e}), falling back to database queries")
            _redis_client = None
    return _redis_client

async def close_redis():
    """Close Redis connection"""
    global _redis_client
    if _redis_client:
        await _redis_client.close()
        _redis_client = None


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against a hash"""
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """Hash a password"""
    return pwd_context.hash(password)


def validate_password_strength(password: str) -> None:
    """Raises 400 if the password doesn't clear the minimum bar: 8+
    characters with at least one letter and one digit. Deliberately not
    stricter than that — most accounts on this platform belong to parents
    and support staff on shared or older devices, where heavier complexity
    rules mostly just push people into writing the password down."""
    if len(password) < 8:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password must be at least 8 characters")
    if not re.search(r"[A-Za-z]", password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password must include at least one letter")
    if not re.search(r"\d", password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password must include at least one number")


def encrypt_onboarding_password(raw_password: str) -> str:
    """User.plain_text_password is only ever a not-yet-first-logged-in
    portal account's generated password, kept so an admin can look it up
    again to hand over — but it must not sit in the database in the
    clear. Reuses services/ai_key_crypto.py's Fernet scheme, the same one
    already used for webhook secrets and QuickBooks tokens."""
    from services.ai_key_crypto import encrypt_api_key
    return encrypt_api_key(raw_password)


def decrypt_onboarding_password(stored_value: Optional[str]) -> Optional[str]:
    """Best-effort: rows written before this field was encrypted still
    hold genuine plaintext, so a decrypt failure there means "not yet
    migrated", not corruption — see scripts/encrypt_legacy_passwords.py
    for the one-time backfill. Falls back to the raw stored value in that
    case rather than hiding it, so admin-facing "view credentials" screens
    don't regress for accounts created before this change."""
    if not stored_value:
        return None
    from services.ai_key_crypto import decrypt_api_key
    try:
        return decrypt_api_key(stored_value)
    except Exception:
        return stored_value


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a JWT access token. Every token carries `iat` (issued-at) and
    a unique `jti` — `jti` is what a single explicit /auth/logout blacklists
    in Redis (see revoke_token), and `iat` is what lets a password change
    invalidate every OTHER token already out there at once, by comparing
    against User.sessions_valid_after in get_current_user, without needing
    to enumerate or blacklist tokens individually."""
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    expire = now + (expires_delta or timedelta(minutes=settings.access_token_expire_minutes))
    to_encode.update({"exp": expire, "iat": now, "jti": str(uuid.uuid4())})
    return jwt.encode(to_encode, settings.secret_key, algorithm=settings.algorithm)


async def revoke_token(jti: str, exp_timestamp: float) -> None:
    """Blacklists one token's jti until its own natural expiry — no reason
    to keep the blacklist entry around any longer than the token itself
    would've been valid for."""
    redis_client = await get_redis()
    if not redis_client:
        logger.warning("Redis unavailable — logout could not blacklist the token; it remains valid until it expires naturally")
        return
    ttl = max(int(exp_timestamp - datetime.now(timezone.utc).timestamp()), 1)
    try:
        await redis_client.setex(f"revoked_token:{jti}", ttl, "1")
    except Exception as e:
        logger.warning(f"Failed to blacklist token on logout: {e}")


async def check_login_allowed(session: AsyncSession, email: str) -> None:
    """Raises 423 if this email has FAILED_LOGIN_MAX_ATTEMPTS+ failed
    logins within the trailing FAILED_LOGIN_WINDOW_MINUTES. Checked BEFORE
    password verification on every attempt, so reaching the threshold
    locks the email out even if the very next guess would have been
    correct — a locked-out account can't be argued out of lockout by
    finally getting the password right."""
    from models.login_attempt import LoginAttempt
    cutoff = datetime.utcnow() - timedelta(minutes=FAILED_LOGIN_WINDOW_MINUTES)
    result = await session.execute(
        select(LoginAttempt.id).where(
            LoginAttempt.email_attempted == email,
            LoginAttempt.success == False,  # noqa: E712
            LoginAttempt.created_at >= cutoff,
        ).limit(FAILED_LOGIN_MAX_ATTEMPTS)
    )
    if len(result.all()) >= FAILED_LOGIN_MAX_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=f"Too many failed login attempts for this account. Try again in {FAILED_LOGIN_WINDOW_MINUTES} minutes.",
        )


async def check_login_rate_limit(ip: Optional[str]) -> None:
    """Coarser, IP-based complement to check_login_allowed's per-email
    lockout — catches a single source spraying guesses across many
    different emails, which the per-email check alone wouldn't."""
    if not ip:
        return
    redis_client = await get_redis()
    if not redis_client:
        return
    key = f"login_rl:{ip}"
    try:
        count = await redis_client.incr(key)
        if count == 1:
            await redis_client.expire(key, 3600)
        if count > LOGIN_RATE_LIMIT_PER_HOUR:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many login attempts from this network. Please try again later.")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Login rate limit check failed ({e}), allowing request")


async def record_login_attempt(
    session: AsyncSession, email: str, success: bool,
    user_id: Optional[str] = None, failure_reason: Optional[str] = None,
    request: Optional[Request] = None,
) -> None:
    from models.login_attempt import LoginAttempt
    try:
        session.add(LoginAttempt(
            email_attempted=email, user_id=user_id, success=success, failure_reason=failure_reason,
            ip_address=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        ))
        await session.commit()
    except Exception as e:
        logger.error(f"Failed to record login attempt for {email}: {e}")



KIOSK_IDLE_TIMEOUT_MINUTES = 3


async def _resolve_kiosk_session(
    token: str, request: Request, session: AsyncSession, credentials_exception: HTTPException,
) -> User:
    from models.security import KioskSession
    from models.school import School

    if not request.url.path.startswith("/api/canteen-wallet/"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Kiosk sessions are for the canteen only")

    result = await session.execute(select(KioskSession).where(KioskSession.session_token == token))
    kiosk_session = result.scalar_one_or_none()
    if not kiosk_session or not kiosk_session.is_active:
        raise credentials_exception

    now = datetime.utcnow()
    if kiosk_session.expires_at < now:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Kiosk session expired — please scan your ID card again")
    if kiosk_session.last_seen_at < now - timedelta(minutes=KIOSK_IDLE_TIMEOUT_MINUTES):
        kiosk_session.is_active = False
        session.add(kiosk_session)
        await session.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Kiosk session timed out from inactivity — please scan your ID card again")

    kiosk_session.last_seen_at = now
    session.add(kiosk_session)
    await session.commit()

    result = await session.execute(select(User).where(User.id == kiosk_session.user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise credentials_exception

    school_result = await session.execute(
        select(School.access_suspended).where(School.id == kiosk_session.school_id)
    )
    school_row = school_result.first()
    if school_row and school_row[0]:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail="Access is unavailable for your school until the outstanding payment is made.")

    return user


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    session: AsyncSession = Depends(get_session)
) -> User:
    """Get the current authenticated user from JWT token with caching"""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    token = credentials.credentials

    # Kiosk sessions (routers/kiosk.py) are opaque DB-backed tokens, not
    # JWTs — a shared-device student login, deliberately kept out of the
    # JWT/jti/sessions_valid_after machinery below rather than retrofitting
    # a `type` claim into every token this app issues. Module-restricted
    # here (not per-endpoint) to /api/canteen-wallet/* — kiosks are for
    # e-canteen self-checkout only (grades/attendance/assignments were
    # deliberately dropped; that's for home/library instead). Within that
    # module, each endpoint's own require_roles(...) still applies exactly
    # as it would for a normal STUDENT session, so this only needs to gate
    # which module a kiosk token can reach, not which action within it.
    if token.startswith("kiosk_"):
        return await _resolve_kiosk_session(token, request, session, credentials_exception)

    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        user_id: str = payload.get("sub")
        token_jti = payload.get("jti")
        token_iat = payload.get("iat")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    # 🚀 OPTIMIZATION: Try cache first (0.1ms vs 5-10ms DB hit)
    redis_client = await get_redis()

    # Explicit-logout check — only tokens issued after create_access_token
    # started stamping jti carry one; a token from before that change just
    # skips this check and is still bound by expiry + sessions_valid_after below.
    if token_jti and redis_client:
        try:
            if await redis_client.get(f"revoked_token:{token_jti}"):
                raise credentials_exception
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"Token revocation check failed ({e}), allowing request")

    cache_key = f"user:{user_id}"
    user = None

    if redis_client:
        try:
            cached_user = await redis_client.get(cache_key)
            if cached_user:
                try:
                    user_dict = json.loads(cached_user)
                    # Normalize role back to the UserRole ENUM INSTANCE, not a plain
                    # string. table=True SQLModel constructors skip validation, so
                    # whatever goes in here is exactly what every call site sees —
                    # a string role would make `.value`/`.name` access crash downstream.
                    raw_role = user_dict.get("role")
                    try:
                        user_dict["role"] = UserRole(raw_role)
                    except ValueError:
                        try:
                            # Stale cache with uppercase enum NAME (e.g. "SCHOOL_ADMIN")
                            user_dict["role"] = UserRole[raw_role]
                        except (KeyError, TypeError):
                            # Unrecognizable role: treat the cache entry as corrupt
                            # and fall through to a fresh DB load.
                            raise ValueError(f"Unrecognizable cached role: {raw_role!r}")
                    # Reconstruct User object from cached dict
                    user = User(**user_dict)
                    logger.debug(f"User {user_id} loaded from cache")
                except Exception as e:
                    logger.warning(f"Error deserializing cached user: {e}")
                    user = None
        except Exception as e:
            logger.warning(f"Redis cache read error: {e}")
    
    # Cache miss or Redis unavailable - query database
    if user is None:
        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        
        if user is None:
            raise credentials_exception
        
        # Cache the user for 15 minutes
        if redis_client:
            try:
                user_dict = {
                    "id": str(user.id),
                    "email": user.email,
                    "role": user.role.value if user.role else None,
                    "is_active": user.is_active,
                    "school_id": str(user.school_id) if user.school_id else None,
                    "campus_id": str(user.campus_id) if user.campus_id else None,
                    "role_id": str(user.role_id) if user.role_id else None,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                }
                await redis_client.setex(
                    cache_key,
                    900,  # 15 minutes TTL
                    json.dumps(user_dict)
                )
                logger.debug(f"User {user_id} cached for 15 minutes")
            except Exception as e:
                logger.warning(f"Redis cache write error: {e}")
    
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account is disabled")

    # Billing enforcement — a super admin's manual "cut off access" switch.
    # Deliberately NOT cached (unlike the user object above): this is a
    # security boundary, and stale "still allowed" reads are exactly the
    # kind of bug caching would introduce here. It's one lightweight
    # indexed-by-primary-key lookup, only for non-super-admin school users.
    if user.role != UserRole.SUPER_ADMIN and user.school_id:
        school_result = await session.execute(
            select(School.access_suspended, School.access_suspended_reason).where(School.id == user.school_id)
        )
        school_row = school_result.first()
        if school_row and school_row[0]:
            reason = (school_row[1] or "").strip()
            detail = "All modules are unavailable for your school until the outstanding payment is made."
            if reason:
                if reason[-1] not in ".!?":
                    reason += "."
                detail += f" {reason}"
            detail += " Contact your school administrator or Campusio support to resolve this."
            raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=detail)

    # Forced-logout-everywhere check — same "deliberately not cached"
    # reasoning as billing enforcement above: a token issued before the
    # user's last password change (or an admin-triggered "log out
    # everywhere") must stop working immediately, not after the cached
    # user entry happens to expire. One indexed-by-primary-key lookup.
    if token_iat is not None:
        valid_after_result = await session.execute(
            select(User.sessions_valid_after).where(User.id == user_id)
        )
        valid_after = valid_after_result.scalar_one_or_none()
        if valid_after is not None:
            token_iat_dt = datetime.fromtimestamp(token_iat, tz=timezone.utc)
            if valid_after.tzinfo is None:
                valid_after = valid_after.replace(tzinfo=timezone.utc)
            if token_iat_dt < valid_after:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Your session has expired — please log in again",
                    headers={"WWW-Authenticate": "Bearer"},
                )

    return user


def require_roles(*roles: UserRole):
    """Dependency to require specific roles"""
    async def role_checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in roles:
            logger.warning(
                f"[403] Role check failed: user={current_user.email} "
                f"role={repr(current_user.role)} type={type(current_user.role).__name__} "
                f"required={[r.value for r in roles]}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Required roles: {[r.value for r in roles]}"
            )
        return current_user
    return role_checker


def require_permission(code: str):
    """Dependency to require a specific fine-grained permission (see
    models/rbac.py). Coexists with require_roles: new or migrated endpoints
    use this, everything else keeps using require_roles unmodified — both
    read the same current_user and can appear side by side across routers
    indefinitely.
    """
    async def permission_checker(
        current_user: User = Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> User:
        from services.permission_service import get_user_permissions  # deferred: avoids import cycle

        permissions = await get_user_permissions(session, current_user)
        if code not in permissions:
            logger.warning(
                f"[403] Permission check failed: user={current_user.email} "
                f"required={code!r}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Required permission: {code}"
            )
        return current_user
    return permission_checker


async def get_api_key_context(
    x_api_key: str = Header(..., alias="X-API-Key"),
    session: AsyncSession = Depends(get_session),
):
    """Resolves an `X-API-Key` header to its scoped ApiKeyContext (see
    services/api_key_service.py, models/integrations.py). This is a
    separate auth path from get_current_user/JWT — for third-party
    integrators calling routers/public_api.py, not human users.
    """
    from services.api_key_service import verify_and_resolve  # deferred: avoids import cycle

    context = await verify_and_resolve(x_api_key, session)
    if context is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid, revoked, or expired API key",
        )
    return context


def require_api_scope(code: str):
    """Dependency to require a specific scope on the calling API key —
    the API-key-auth counterpart to require_permission(), checked against
    ApiKeyContext.scopes instead of a user's resolved permission set."""
    async def scope_checker(context=Depends(get_api_key_context)):
        if code not in context.scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"API key is missing required scope: {code}",
            )
        return context
    return scope_checker


def require_school_access(current_user: User = Depends(get_current_user)) -> User:
    """Require user to have school_id (non-super admin)"""
    if current_user.role == UserRole.SUPER_ADMIN:
        return current_user
    if not current_user.school_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not associated with any school"
        )
    return current_user
