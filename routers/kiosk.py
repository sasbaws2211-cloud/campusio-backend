"""Kiosk Login Router — shared-device e-canteen self-checkout.

A student without a personal phone/tablet scans their ID card's QR (its
card_number, routers/id_cards.py's _new_card) on a school-owned shared
tablet, then enters their 4-digit PIN, to open a short-lived session
scoped to e-canteen self-checkout only — see auth.py's get_current_user
for how the resulting `kiosk_`-prefixed session_token is accepted
(/api/canteen-wallet/* only, any method within it gated by that module's
own per-endpoint roles) across the existing canteen-wallet endpoints with
no changes needed there. Kiosk sessions deliberately cannot reach the
student portal (grades/attendance/assignments/etc.) — that's for the
student to check at home or in the library instead.

Every endpoint here is intentionally public (no Depends(get_current_user))
— that's the whole point of the flow, so it needs its own rate limiting
rather than relying on any per-user auth boundary.
"""
import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlmodel import SQLModel, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_password_hash, verify_password, get_redis, get_current_user
from database import get_session
from models.certificates import IDCard, IDCardStatus, PersonType
from models.student import Student
from models.security import KioskPendingScan, KioskSession
from routers.parent import verify_child_access
from models.user import User, UserRole

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/kiosk", tags=["Kiosk Login"])

PENDING_SCAN_TTL_MINUTES = 2
SESSION_TTL_MINUTES = 20
MAX_PIN_ATTEMPTS = 5
SCAN_RATE_LIMIT_PER_HOUR_PER_IP = 30
SCAN_RATE_LIMIT_PER_HOUR_PER_CARD = 10


async def _check_rate_limit(key: str, limit: int) -> None:
    """Best-effort Redis counter — mirrors auth.py's check_login_rate_limit.
    Fails open (allows the request) if Redis is unavailable, same tradeoff
    made everywhere else in this codebase's rate limiting."""
    redis_client = await get_redis()
    if not redis_client:
        return
    try:
        count = await redis_client.incr(key)
        if count == 1:
            await redis_client.expire(key, 3600)
        if count > limit:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many attempts. Please try again later.")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Kiosk rate limit check failed ({e}), allowing request")


class KioskScanRequest(SQLModel):
    card_number: str


class KioskVerifyPinRequest(SQLModel):
    pending_token: str
    pin: str


class KioskLogoutRequest(SQLModel):
    session_token: str


class KioskPinUpdateRequest(SQLModel):
    pin: str


def _validate_pin_format(pin: str) -> None:
    if not (pin and pin.isdigit() and len(pin) == 4):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="PIN must be exactly 4 digits")


@router.post("/scan", response_model=dict)
async def kiosk_scan(
    data: KioskScanRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Step 1: resolve a scanned/entered card_number to a student and issue
    a short-lived pending_token for the PIN step. Deliberately vague error
    messages — this is a public endpoint, so it shouldn't reveal whether a
    given card_number exists at all."""
    ip = request.client.host if request.client else None
    if ip:
        await _check_rate_limit(f"kiosk_scan_ip:{ip}", SCAN_RATE_LIMIT_PER_HOUR_PER_IP)
    await _check_rate_limit(f"kiosk_scan_card:{data.card_number}", SCAN_RATE_LIMIT_PER_HOUR_PER_CARD)

    invalid_card = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Card not recognized. Ask staff for help.")

    result = await session.execute(
        select(IDCard).where(IDCard.card_number == data.card_number, IDCard.person_type == PersonType.STUDENT)
    )
    card = result.scalar_one_or_none()
    if not card or card.status != IDCardStatus.ACTIVE:
        raise invalid_card

    result = await session.execute(select(Student).where(Student.id == card.person_id))
    student = result.scalar_one_or_none()
    if not student or not student.user_id:
        raise invalid_card

    if not student.kiosk_pin_hash:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Kiosk login isn't set up for this student yet. Ask a parent or the school office to set a PIN.",
        )

    pending = KioskPendingScan(
        pending_token=secrets.token_urlsafe(32),
        school_id=card.school_id,
        student_id=student.id,
        user_id=student.user_id,
        expires_at=datetime.utcnow() + timedelta(minutes=PENDING_SCAN_TTL_MINUTES),
    )
    session.add(pending)
    await session.commit()

    return {
        "pending_token": pending.pending_token,
        "expires_at": pending.expires_at.isoformat(),
        "student_first_name": student.first_name,
    }


@router.post("/verify-pin", response_model=dict)
async def kiosk_verify_pin(
    data: KioskVerifyPinRequest,
    session: AsyncSession = Depends(get_session),
):
    """Step 2: check the PIN against the student the pending_token resolved
    to, and on success mint the actual KioskSession."""
    result = await session.execute(select(KioskPendingScan).where(KioskPendingScan.pending_token == data.pending_token))
    pending = result.scalar_one_or_none()
    expired_exc = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="This scan has expired. Please scan your ID card again.")
    if not pending or pending.expires_at < datetime.utcnow():
        raise expired_exc

    if pending.pin_attempts >= MAX_PIN_ATTEMPTS:
        await session.delete(pending)
        await session.commit()
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail="Too many incorrect PIN attempts. Please scan your ID card again.")

    result = await session.execute(select(Student).where(Student.id == pending.student_id))
    student = result.scalar_one_or_none()
    if not student or not student.kiosk_pin_hash:
        await session.delete(pending)
        await session.commit()
        raise expired_exc

    if not verify_password(data.pin, student.kiosk_pin_hash):
        pending.pin_attempts += 1
        session.add(pending)
        await session.commit()
        remaining = MAX_PIN_ATTEMPTS - pending.pin_attempts
        if remaining <= 0:
            raise HTTPException(status_code=status.HTTP_423_LOCKED, detail="Too many incorrect PIN attempts. Please scan your ID card again.")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Incorrect PIN. {remaining} attempt(s) remaining.")

    kiosk_session = KioskSession(
        session_token=f"kiosk_{secrets.token_urlsafe(32)}",
        school_id=pending.school_id,
        student_id=pending.student_id,
        user_id=pending.user_id,
        expires_at=datetime.utcnow() + timedelta(minutes=SESSION_TTL_MINUTES),
    )
    session.add(kiosk_session)
    await session.delete(pending)
    await session.commit()

    return {
        "session_token": kiosk_session.session_token,
        "expires_at": kiosk_session.expires_at.isoformat(),
        "student_first_name": student.first_name,
    }


@router.post("/logout", response_model=dict)
async def kiosk_logout(
    data: KioskLogoutRequest,
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(KioskSession).where(KioskSession.session_token == data.session_token))
    kiosk_session = result.scalar_one_or_none()
    if kiosk_session and kiosk_session.is_active:
        kiosk_session.is_active = False
        kiosk_session.ended_at = datetime.utcnow()
        session.add(kiosk_session)
        await session.commit()
    return {"ok": True}


@router.get("/session", response_model=dict)
async def kiosk_session_status(
    session_token: str,
    session: AsyncSession = Depends(get_session),
):
    """Liveness ping so the frontend can proactively bounce to the scan
    screen once the backend has already expired the session, instead of
    only discovering it on the next failed portal request. Deliberately
    read-only — does NOT refresh last_seen_at, so polling this can't be
    used as a backdoor keep-alive around the real idle timeout."""
    result = await session.execute(select(KioskSession).where(KioskSession.session_token == session_token))
    kiosk_session = result.scalar_one_or_none()
    now = datetime.utcnow()
    valid = bool(
        kiosk_session
        and kiosk_session.is_active
        and kiosk_session.expires_at >= now
        and kiosk_session.last_seen_at >= now - timedelta(minutes=3)
    )
    return {"valid": valid, "expires_at": kiosk_session.expires_at.isoformat() if valid else None}


# ── PIN management (set/reset) ────────────────────────────────────────────
# Lives here rather than parent.py/student_portal.py since it's entirely
# about the kiosk feature, even though the caller is authenticated normally.

STAFF_PIN_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.TEACHER)


@router.put("/students/{student_id}/pin", response_model=dict)
async def set_kiosk_pin(
    student_id: str,
    data: KioskPinUpdateRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Set/reset a student's kiosk PIN. Parents may do this for their own
    linked child (reuses parent.py's verify_child_access); school admins
    and teachers may do it for any student at their school, as a
    staff-assisted fallback for parents who haven't set one up."""
    _validate_pin_format(data.pin)

    if current_user.role == UserRole.PARENT:
        student = await verify_child_access(student_id, current_user, session)
    elif current_user.role in STAFF_PIN_ROLES:
        result = await session.execute(select(Student).where(Student.id == student_id, Student.school_id == current_user.school_id))
        student = result.scalar_one_or_none()
        if not student:
            raise HTTPException(status_code=404, detail="Student not found")
    else:
        raise HTTPException(status_code=403, detail="Access denied")

    student.kiosk_pin_hash = get_password_hash(data.pin)
    session.add(student)
    await session.commit()
    return {"ok": True}
