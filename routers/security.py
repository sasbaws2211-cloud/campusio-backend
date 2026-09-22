"""Student Security Module Router"""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status, Query, UploadFile, File
from sqlmodel import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import List, Optional
import asyncio
import json
import uuid
import logging
from jose import jwt, JWTError

from services.document_service import MAX_FILE_SIZE_BYTES

from services.broadcaster import broadcaster
from config import get_settings

from models.security import (
    StudentSecurityProfile, StudentSecurityProfileCreate, StudentSecurityProfileUpdate,
    DailyQRToken, QRTokenType,
    SecurityScanLog, ScanResult,
    ArrivalEvent, ArrivalEventType,
    ParentNote, ParentNoteCreate,
    LiveBusLocation, LiveBusLocationCreate,
    CollectorTrackingSession,
    CollectorLiveLocation, CollectorLocationCreate,
    ArrivalStatus,
    SchoolQRDispatch,
    AuthorizedPickupPerson, AuthorizedPickupPersonCreate, AuthorizedPickupPersonUpdate,
    StudentLocationLog, UpdateLocationRequest, SetReleaseHoldRequest,
    SecurityIncident, AcknowledgeIncidentRequest, SetLockdownRequest,
)
from models.student import Student, Parent, StudentParent
from models.classroom import Class
from models.transport import Route as TransportRoute, Vehicle, DriverStaff, RouteStop
from models.staff import Staff
from models.user import User, UserRole
from models.communication import MessageType
from database import get_session
from auth import get_current_user, require_roles
from models.school import School
from services.email_service import email_service
from services import gate_attendance_service, parent_notification_service, security_alert_service
from services.sms_service import sms_service
from routers.parent import get_parent_children_ids
from routers.transport import get_driver_route_ids
from services.plan_gating import require_plan_feature
from models.certificates import IDCard, IDCardStatus, PersonType
from services.geo_utils import haversine_distance_meters

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/security", tags=["Student Security"])


# ── Request bodies (all mutating endpoints take JSON bodies, never bare
#    scalar params — FastAPI would silently read those from the query string) ──

from sqlmodel import SQLModel


class SchoolScopedRequest(SQLModel):
    school_id: str


class GenerateDailyRequest(SQLModel):
    school_id: str
    idempotency_key: Optional[str] = None
from sqlalchemy.exc import IntegrityError


class VerifyQRRequest(SQLModel):
    token: str
    gate_lat: Optional[float] = None
    gate_lng: Optional[float] = None
    # Who physically presented the code — captured at scan time so an
    # "authorized" scan records who showed up, not just that a valid code
    # was presented. Required (see verify_qr_token) when the student has an
    # active custody restriction on file, since that's the highest-risk case.
    collector_name: Optional[str] = None
    # Optional: the officer picked a specific AuthorizedPickupPerson from
    # the reference list shown on their screen (rather than typing a free
    # name) — when given, that person's name is used as collector_name and
    # AuthorizedPickupPerson.photo_required is enforced (see verify_qr_token).
    person_id: Optional[str] = None


class DispatchRouteRequest(SchoolScopedRequest):
    # When given, ONLY these enrolled students are marked EN_ROUTE_BUS —
    # everyone else on the route stays in their current status instead of
    # being bulk-marked "en route" on trust alone. Omit to keep the old
    # all-enrolled-students behavior (e.g. a school not yet doing a
    # boarding headcount).
    boarded_student_ids: Optional[List[str]] = None


class UpdateArrivalStatusRequest(SQLModel):
    arrival_status: ArrivalStatus


class OnMyWayRequest(SQLModel):
    eta_minutes: Optional[int] = None

ADMIN_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)
OFFICER_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.SECURITY_OFFICER)

PICKUP_PERSON_PHOTO_DIR = Path("uploads/pickup_persons")
ALLOWED_PHOTO_CONTENT_TYPES = {"image/jpeg", "image/png"}

settings = get_settings()


# ── Helpers ───────────────────────────────────────────────────────────────────

def today_str() -> str:
    return date.today().isoformat()


def assert_school_access(current_user: User, school_id: str) -> None:
    """Reject cross-school access for non-super-admins.

    Every other router in this codebase scopes school_id-bearing requests to
    current_user.school_id; this module previously trusted the client-supplied
    school_id outright, letting one school's admin/officer view or mutate
    another school's pickup data.
    """
    if current_user.role != UserRole.SUPER_ADMIN and current_user.school_id != school_id:
        raise HTTPException(status_code=403, detail="Access denied for this school")


async def assert_parent_of_student(current_user: User, student_id: str, db: AsyncSession) -> None:
    """Restrict parent-facing student endpoints to that parent's own linked children.

    Staff roles are unrestricted here since they may legitimately act on a
    parent's behalf. Found while wiring up QR sharing: get_student_qr_token and
    email_student_qr previously let any authenticated parent pull or email out
    ANY student's live pickup-authorization QR just by knowing their student_id.
    """
    if current_user.role != UserRole.PARENT:
        return
    child_ids = await get_parent_children_ids(current_user, db)
    if student_id not in child_ids:
        raise HTTPException(status_code=403, detail="Not authorized for this student")


async def assert_staff_school_access_for_student(current_user: User, student_id: str, db: AsyncSession) -> None:
    """Tenant check for the staff side of endpoints that also accept a
    parent caller via assert_parent_of_student. assert_parent_of_student
    only restricts PARENT callers to their own linked children and is a
    deliberate no-op for every staff role ("staff may legitimately act on a
    parent's behalf") -- but that no-op previously meant a staff account
    from ANY school could call these endpoints for a student in a
    DIFFERENT school, since nothing else checked school_id. Call this
    alongside assert_parent_of_student wherever a student_id-keyed
    endpoint is meant to be staff-usable-for-any-student but only within
    the caller's own school."""
    if current_user.role == UserRole.PARENT:
        return
    student = await db.get(Student, student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    assert_school_access(current_user, student.school_id)


async def _parent_pickup_restriction(current_user: User, student_id: str, db: AsyncSession) -> Optional["StudentParent"]:
    """Returns the StudentParent link if it exists and is pickup-restricted,
    else None — a non-raising helper so a multi-child batch action (e.g.
    email_all_children_qr) can skip just the restricted child instead of
    failing the whole request. See assert_parent_pickup_allowed for the
    single-child hard-deny wrapper."""
    if current_user.role != UserRole.PARENT:
        return None
    parent_result = await db.exec(select(Parent).where(Parent.user_id == current_user.id))
    parent = parent_result.first()
    if not parent:
        return None
    link_result = await db.exec(
        select(StudentParent).where(
            StudentParent.student_id == student_id,
            StudentParent.parent_id == parent.id,
        )
    )
    link = link_result.first()
    return link if (link and link.is_pickup_restricted) else None


async def assert_parent_pickup_allowed(current_user: User, student_id: str, db: AsyncSession) -> None:
    """Hard-deny pickup-related actions (QR issuance, sharing the pickup QR,
    "on my way") for a parent whose StudentParent link has been marked
    pickup-restricted by an admin acting on a court order — see
    StudentParent.is_pickup_restricted. Deliberately separate from
    assert_parent_of_student: a restricted parent can still view their
    child's status and leave notes, only actually picking up is blocked.
    Staff roles are unrestricted here, same as assert_parent_of_student."""
    link = await _parent_pickup_restriction(current_user, student_id, db)
    if link:
        try:
            await broadcaster.publish({
                "type": "custody_restriction_blocked",
                "school_id": current_user.school_id,
                "student_id": student_id,
                "parent_id": link.parent_id,
            })
        except Exception:
            logger.exception("Failed to publish custody_restriction_blocked event")
        # Persist + proactively alert school staff — previously this only
        # broadcast (nobody notified unless a screen was open) and left no
        # record a pattern (e.g. repeated attempts) could ever be seen in.
        try:
            await security_alert_service.raise_incident(
                db, current_user.school_id or "", "custody_restriction_attempt",
                sms_message="Security alert: a pickup-restricted parent account attempted a pickup action for a student. Review the security incident log.",
                student_id=student_id, related_user_id=link.parent_id,
                details=link.restriction_reason,
            )
        except Exception:
            logger.exception("Failed to raise custody_restriction_attempt incident")
        raise HTTPException(
            status_code=403,
            detail=f"Pickup access is restricted for this account. {link.restriction_reason or 'Contact school administration.'}",
        )


async def assert_no_pickup_lockdown(db: AsyncSession, school_id: str) -> None:
    """Hard-deny release-adjacent actions while a school-wide emergency
    pickup lockdown is active (School.pickup_lockdown_active) — set via
    POST /security/admin/lockdown for a crisis where NO student should be
    released regardless of any individual QR/hold state. Separate from the
    per-student release_hold check below since this is school-wide, not
    per-student."""
    school = await db.get(School, school_id)
    if school and school.pickup_lockdown_active:
        raise HTTPException(
            status_code=403,
            detail=f"This school has an active emergency pickup lockdown — no students can be released right now. {school.pickup_lockdown_reason or 'Contact school administration.'}",
        )


async def assert_release_allowed(db: AsyncSession, profile: "StudentSecurityProfile") -> None:
    """Hard-deny releasing a student who has an active campus-exit hold, or
    whose school has an active emergency lockdown — checked at every
    release-adjacent path (a QR scan in verify_qr_token, a staff-driven
    status change in update_student_status, a parent's own "on my way" and
    "confirm safe arrival") so neither guard can be bypassed by whichever
    path doesn't check it. The only way past the per-student hold is for an
    admin/security officer to first lift it via its own dedicated endpoint
    (POST .../release-hold with active=false) — never a side effect of a
    normal pickup action."""
    if profile.release_hold:
        raise HTTPException(
            status_code=403,
            detail=f"This student has an active release hold and cannot be released. {profile.release_hold_reason or 'Contact school administration.'}",
        )
    await assert_no_pickup_lockdown(db, profile.school_id)


def reset_profile_if_stale(profile: "StudentSecurityProfile", now: datetime) -> bool:
    """Roll a profile back to PENDING if its status is left over from a prior day.

    Nothing currently calls POST /security/admin/reset-day on a schedule, so
    without this, a student marked safe_confirmed yesterday would stay stuck
    in that state and never reappear in today's pickup queue. This is applied
    lazily wherever the roster is read, so it self-heals regardless of when
    (or whether) an explicit reset is triggered.
    """
    if profile.arrival_status == ArrivalStatus.PENDING and not profile.parent_on_the_way:
        return False
    reference = profile.updated_at or profile.arrival_time
    if not reference or reference.date() == now.date():
        return False
    profile.arrival_status = ArrivalStatus.PENDING
    profile.arrival_time = None
    profile.confirmed_at = None
    profile.parent_on_the_way = False
    profile.parent_on_the_way_at = None
    profile.parent_eta_minutes = None
    profile.parent_arrived_at = None
    profile.queue_position = None
    profile.updated_at = now
    return True


def token_expires_at() -> datetime:
    """Tokens expire at midnight of the current day"""
    tomorrow = date.today() + timedelta(days=1)
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, 0, 0, 0)


async def get_or_create_qr_token(db: AsyncSession, student_id: str):
    """Get (or lazily create) today's parent-pickup QR token for a student.
    Returns (token, student), or (None, None) if the student doesn't exist."""
    student_result = await db.exec(select(Student).where(Student.id == student_id))
    student = student_result.first()
    if not student:
        return None, None

    today = today_str()
    result = await db.exec(
        select(DailyQRToken).where(
            and_(
                DailyQRToken.student_id == student_id,
                DailyQRToken.issued_date == today,
                DailyQRToken.token_type == QRTokenType.PARENT_PICKUP,
            )
        )
    )
    token = result.first()
    if not token:
        token = DailyQRToken(
            token=str(uuid.uuid4()),
            token_type=QRTokenType.PARENT_PICKUP,
            school_id=str(student.school_id),
            student_id=student_id,
            issued_date=today,
            expires_at=token_expires_at(),
        )
        db.add(token)
        await db.commit()
        await db.refresh(token)

    return token, student


async def get_parent_emails_for_student(db: AsyncSession, student_id: str) -> List[str]:
    """Return all parent email addresses linked to a student."""
    links_result = await db.exec(
        select(StudentParent).where(StudentParent.student_id == student_id)
    )
    links = links_result.all()
    if not links:
        return []

    parent_ids = [link.parent_id for link in links if getattr(link, "parent_id", None)]
    if not parent_ids:
        return []

    parents_result = await db.exec(select(Parent).where(Parent.id.in_(parent_ids)))
    emails = [parent.email for parent in parents_result.all() if getattr(parent, "email", None)]
    return list(dict.fromkeys(emails))


async def get_pickup_person_card_statuses(db: AsyncSession, person_ids: List[str]) -> dict:
    """IDCard.status for each AuthorizedPickupPerson that has one issued —
    previously a pickup-person's card could be marked LOST/REVOKED
    (routers/id_cards.py) with zero effect on the actual pickup-verification
    flow, since nothing here ever looked at IDCard at all. Surfacing the
    status wherever the pickup-person reference list is shown at least
    gives the officer's own visual check some teeth: a revoked card is
    flagged right on the screen where they're deciding whether to trust the
    person in front of them, instead of that revocation being invisible
    outside the ID Cards admin page."""
    if not person_ids:
        return {}
    result = await db.execute(
        select(IDCard.person_id, IDCard.status).where(
            IDCard.person_type == PersonType.PICKUP_PERSON,
            IDCard.person_id.in_(person_ids),
        )
    )
    return dict(result.all())


async def get_restricted_student_ids(db: AsyncSession, student_ids: List[str]) -> set:
    """Which of these students have at least one pickup-restricted parent
    link — previously is_pickup_restricted never appeared on ANY
    officer-facing screen (get_all_students_status, get_admin_overview,
    get_pickup_queue), so the only place it was ever visible was buried on
    a per-parent StudentParent record nobody views during a live pickup."""
    if not student_ids:
        return set()
    result = await db.execute(
        select(StudentParent.student_id).where(
            StudentParent.student_id.in_(student_ids),
            StudentParent.is_pickup_restricted == True,  # noqa: E712
        )
    )
    return set(result.scalars().all())


async def get_parent_phones_for_student(db: AsyncSession, student_id: str) -> List[str]:
    """Return all parent phone numbers linked to a student."""
    links_result = await db.exec(
        select(StudentParent).where(StudentParent.student_id == student_id)
    )
    links = links_result.all()
    if not links:
        return []

    parent_ids = [link.parent_id for link in links if getattr(link, "parent_id", None)]
    if not parent_ids:
        return []

    parents_result = await db.exec(select(Parent).where(Parent.id.in_(parent_ids)))
    phones = [parent.phone for parent in parents_result.all() if getattr(parent, "phone", None)]
    return list(dict.fromkeys(phones))


# ── Student Security Profiles ─────────────────────────────────────────────────

@router.post("/profiles", response_model=StudentSecurityProfile)
async def create_security_profile(
    data: StudentSecurityProfileCreate,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
):
    """Create a security profile for a student"""
    assert_school_access(current_user, data.school_id)
    student_result = await db.exec(
        select(Student).where(Student.id == data.student_id, Student.school_id == data.school_id)
    )
    if not student_result.first():
        raise HTTPException(status_code=404, detail="Student not found in this school")

    existing = await db.exec(
        select(StudentSecurityProfile).where(StudentSecurityProfile.student_id == data.student_id)
    )
    if existing.first():
        raise HTTPException(status_code=400, detail="Security profile already exists for this student")

    profile = StudentSecurityProfile(**data.model_dump())
    db.add(profile)
    await db.commit()
    await db.refresh(profile)
    return profile


@router.get("/profiles/{student_id}", response_model=StudentSecurityProfile)
async def get_security_profile(
    student_id: str,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    result = await db.exec(
        select(StudentSecurityProfile).where(StudentSecurityProfile.student_id == student_id)
    )
    profile = result.first()
    if not profile:
        raise HTTPException(status_code=404, detail="Security profile not found")
    assert_school_access(current_user, profile.school_id)
    return profile


@router.patch("/profiles/{student_id}", response_model=StudentSecurityProfile)
async def update_security_profile(
    student_id: str,
    data: StudentSecurityProfileUpdate,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
):
    result = await db.exec(
        select(StudentSecurityProfile).where(StudentSecurityProfile.student_id == student_id)
    )
    profile = result.first()
    if not profile:
        raise HTTPException(status_code=404, detail="Security profile not found")
    assert_school_access(current_user, profile.school_id)

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(profile, field, value)
    profile.updated_at = datetime.utcnow()

    db.add(profile)
    await db.commit()
    await db.refresh(profile)
    return profile


# ── Campus-Exit Hold ─────────────────────────────────────────────────────────
# "Block a student from leaving campus without permission" — a deliberate,
# reason-required action distinct from the normal pickup flow. See
# assert_release_allowed for where this is enforced.

@router.post("/students/{student_id}/release-hold")
async def set_release_hold(
    student_id: str,
    body: SetReleaseHoldRequest,
    current_user: User = Depends(require_roles(*OFFICER_ROLES)),
    _plan_check: User = Depends(require_plan_feature("student_safety")),
    db: AsyncSession = Depends(get_session),
):
    if not body.reason or not body.reason.strip():
        raise HTTPException(status_code=400, detail="A reason is required to place a release hold")
    result = await db.exec(select(StudentSecurityProfile).where(StudentSecurityProfile.student_id == student_id))
    profile = result.first()
    if not profile:
        raise HTTPException(status_code=404, detail="Security profile not found")

    profile.release_hold = True
    profile.release_hold_reason = body.reason.strip()
    profile.release_hold_set_by = current_user.id
    profile.release_hold_set_at = datetime.utcnow()
    profile.updated_at = datetime.utcnow()
    db.add(profile)
    await db.commit()

    try:
        await broadcaster.publish({
            "type": "release_hold_set",
            "school_id": profile.school_id,
            "student_id": student_id,
            "reason": profile.release_hold_reason,
        })
    except Exception:
        logger.exception("Failed to publish release_hold_set event")

    return {"student_id": student_id, "release_hold": True, "reason": profile.release_hold_reason}


@router.delete("/students/{student_id}/release-hold")
async def clear_release_hold(
    student_id: str,
    current_user: User = Depends(require_roles(*OFFICER_ROLES)),
    _plan_check: User = Depends(require_plan_feature("student_safety")),
    db: AsyncSession = Depends(get_session),
):
    result = await db.exec(select(StudentSecurityProfile).where(StudentSecurityProfile.student_id == student_id))
    profile = result.first()
    if not profile:
        raise HTTPException(status_code=404, detail="Security profile not found")

    # Archive what's being cleared BEFORE wiping it — previously these
    # fields were simply overwritten with no history table, so "who placed
    # this hold, why, and who lifted it" was unanswerable the moment it was
    # cleared. Also, unlike set_release_hold, clearing one fired no event
    # at all — a connected admin screen had no way to know a hold had just
    # been lifted.
    was_active = profile.release_hold
    if was_active:
        try:
            await security_alert_service.log_incident(
                db, profile.school_id, "release_hold_cleared",
                student_id=student_id, related_user_id=current_user.id,
                details=f"Reason was: {profile.release_hold_reason or 'none given'}. Set by {profile.release_hold_set_by} at {profile.release_hold_set_at}.",
            )
        except Exception:
            logger.exception("Failed to log release_hold_cleared incident")

    profile.release_hold = False
    profile.release_hold_reason = None
    profile.release_hold_set_by = None
    profile.release_hold_set_at = None
    profile.updated_at = datetime.utcnow()
    db.add(profile)
    await db.commit()

    if was_active:
        try:
            await broadcaster.publish({
                "type": "release_hold_cleared",
                "school_id": profile.school_id,
                "student_id": student_id,
            })
        except Exception:
            logger.exception("Failed to publish release_hold_cleared event")

    return {"student_id": student_id, "release_hold": False}


# ── Court-Order / Custody Pickup Restriction ────────────────────────────────
# Admin-only (not security officers) — this is a legal designation that
# should be set only against actual court documentation, distinct from the
# day-to-day release-hold above which any officer can set for an immediate
# situation. See assert_parent_pickup_allowed for where this is enforced.

class SetPickupRestrictionRequest(SQLModel):
    is_restricted: bool
    reason: Optional[str] = None


@router.put("/students/{student_id}/parents/{parent_id}/pickup-restriction")
async def set_pickup_restriction(
    student_id: str,
    parent_id: str,
    body: SetPickupRestrictionRequest,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    _plan_check: User = Depends(require_plan_feature("pickup_advanced")),
    db: AsyncSession = Depends(get_session),
):
    if body.is_restricted and not (body.reason or "").strip():
        raise HTTPException(status_code=400, detail="A reason (e.g. the court order reference) is required to restrict pickup access")

    result = await db.exec(
        select(StudentParent).where(StudentParent.student_id == student_id, StudentParent.parent_id == parent_id)
    )
    link = result.first()
    if not link:
        raise HTTPException(status_code=404, detail="This parent is not linked to this student")

    # Archive before overwriting, same reasoning as clear_release_hold —
    # previously lifting a restriction wiped restricted_by/restricted_at
    # with no record of what was cleared or by whom.
    was_restricted = link.is_pickup_restricted
    prior_reason = link.restriction_reason

    link.is_pickup_restricted = body.is_restricted
    link.restriction_reason = body.reason.strip() if body.is_restricted and body.reason else None
    link.restricted_by = current_user.id if body.is_restricted else None
    link.restricted_at = datetime.utcnow() if body.is_restricted else None
    db.add(link)
    await db.commit()

    try:
        if body.is_restricted != was_restricted:
            student = await db.get(Student, student_id)
            school_id = student.school_id if student else (current_user.school_id or "")
            if body.is_restricted:
                await security_alert_service.log_incident(
                    db, school_id, "custody_restriction_set",
                    student_id=student_id, related_user_id=parent_id, details=link.restriction_reason,
                )
            else:
                await security_alert_service.log_incident(
                    db, school_id, "custody_restriction_cleared",
                    student_id=student_id, related_user_id=parent_id,
                    details=f"Prior reason was: {prior_reason or 'none given'}.",
                )
    except Exception:
        logger.exception("Failed to log custody restriction change")

    return {
        "student_id": student_id, "parent_id": parent_id,
        "is_pickup_restricted": link.is_pickup_restricted, "reason": link.restriction_reason,
    }


# ── Authorized Pickup Persons ───────────────────────────────────────────────
# A real multi-person list, replacing the old single flat
# authorized_pickup_name/phone/relationship fields on StudentSecurityProfile
# (kept there for backward compat, no longer the primary record).

@router.get("/students/{student_id}/pickup-persons", response_model=List[dict])
async def list_pickup_persons(
    student_id: str,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("student_safety")),
    db: AsyncSession = Depends(get_session),
):
    """Parents see their own child's list; staff see any student's (same school only)."""
    await assert_parent_of_student(current_user, student_id, db)
    student = await db.get(Student, student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    assert_school_access(current_user, student.school_id)
    result = await db.exec(
        select(AuthorizedPickupPerson)
        .where(AuthorizedPickupPerson.student_id == student_id)
        .order_by(AuthorizedPickupPerson.is_active.desc(), AuthorizedPickupPerson.name)
    )
    persons = result.all()
    card_statuses = await get_pickup_person_card_statuses(db, [p.id for p in persons])
    return [{**p.model_dump(), "card_status": card_statuses.get(p.id)} for p in persons]


@router.post("/students/{student_id}/pickup-persons", response_model=AuthorizedPickupPerson)
async def add_pickup_person(
    student_id: str,
    body: AuthorizedPickupPersonCreate,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("student_safety")),
    db: AsyncSession = Depends(get_session),
):
    """Parents manage their own child's list (self-service, per the standing
    'as parents' preferences change' ask); staff can add on a parent's
    behalf. A pickup-restricted parent cannot add new pickup persons either
    — that would be a straightforward way around their own restriction."""
    await assert_parent_of_student(current_user, student_id, db)
    await assert_parent_pickup_allowed(current_user, student_id, db)
    if current_user.role not in (UserRole.PARENT, *ADMIN_ROLES, UserRole.SECURITY_OFFICER):
        raise HTTPException(status_code=403, detail="Access denied")
    if not body.name or not body.name.strip():
        raise HTTPException(status_code=400, detail="Name is required")

    student = await db.get(Student, student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    assert_school_access(current_user, student.school_id)

    person = AuthorizedPickupPerson(
        school_id=student.school_id, student_id=student_id,
        name=body.name.strip(), phone=body.phone, relationship=body.relationship, notes=body.notes,
        photo_required=body.photo_required, added_by=current_user.id,
    )
    db.add(person)
    await db.commit()
    await db.refresh(person)
    return person


@router.patch("/pickup-persons/{person_id}", response_model=AuthorizedPickupPerson)
async def update_pickup_person(
    person_id: str,
    body: AuthorizedPickupPersonUpdate,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("student_safety")),
    db: AsyncSession = Depends(get_session),
):
    person = await db.get(AuthorizedPickupPerson, person_id)
    if not person:
        raise HTTPException(status_code=404, detail="Pickup person not found")
    await assert_parent_of_student(current_user, person.student_id, db)
    await assert_parent_pickup_allowed(current_user, person.student_id, db)
    if current_user.role not in (UserRole.PARENT, *ADMIN_ROLES, UserRole.SECURITY_OFFICER):
        raise HTTPException(status_code=403, detail="Access denied")
    assert_school_access(current_user, person.school_id)

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(person, field, value)
    person.updated_at = datetime.utcnow()
    db.add(person)
    await db.commit()
    await db.refresh(person)
    return person


@router.delete("/pickup-persons/{person_id}")
async def delete_pickup_person(
    person_id: str,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("student_safety")),
    db: AsyncSession = Depends(get_session),
):
    person = await db.get(AuthorizedPickupPerson, person_id)
    if not person:
        raise HTTPException(status_code=404, detail="Pickup person not found")
    await assert_parent_of_student(current_user, person.student_id, db)
    await assert_parent_pickup_allowed(current_user, person.student_id, db)
    if current_user.role not in (UserRole.PARENT, *ADMIN_ROLES, UserRole.SECURITY_OFFICER):
        raise HTTPException(status_code=403, detail="Access denied")
    assert_school_access(current_user, person.school_id)

    await db.delete(person)
    await db.commit()
    return {"message": "Pickup person removed"}


@router.get("/pickup-persons", response_model=List[dict])
async def list_all_pickup_persons(
    student_id: Optional[str] = None,
    active_only: bool = True,
    current_user: User = Depends(require_roles(*OFFICER_ROLES)),
    _plan_check: User = Depends(require_plan_feature("student_safety")),
    db: AsyncSession = Depends(get_session),
):
    """School-wide roster, unlike list_pickup_persons above (one student at
    a time) — staff-only, since it's cross-student. Used by the ID Cards
    page to pick a pickup person to issue a card for, and by any other
    school-wide reference view. Joins each row to its student's name since
    a bare AuthorizedPickupPerson list gives no context on who they're
    approved to collect."""
    query = select(AuthorizedPickupPerson).where(AuthorizedPickupPerson.school_id == current_user.school_id)
    if student_id:
        query = query.where(AuthorizedPickupPerson.student_id == student_id)
    if active_only:
        query = query.where(AuthorizedPickupPerson.is_active == True)
    result = await db.exec(query.order_by(AuthorizedPickupPerson.name))
    persons = result.all()

    student_ids = {p.student_id for p in persons}
    students_by_id = {}
    if student_ids:
        students_result = await db.exec(select(Student).where(Student.id.in_(student_ids)))
        students_by_id = {s.id: s for s in students_result.all()}

    card_statuses = await get_pickup_person_card_statuses(db, [p.id for p in persons])

    return [
        {
            **person.model_dump(),
            "student_name": (
                f"{students_by_id[person.student_id].first_name} {students_by_id[person.student_id].last_name}"
                if person.student_id in students_by_id else None
            ),
            "card_status": card_statuses.get(person.id),
        }
        for person in persons
    ]


@router.post("/pickup-persons/{person_id}/photo", response_model=dict)
async def upload_pickup_person_photo(
    person_id: str,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("student_safety")),
    db: AsyncSession = Depends(get_session),
):
    """Same pattern as POST /students/{id}/photo — saves to
    uploads/pickup_persons/{school_id}/{person_id}/{uuid}_{filename} and
    stamps AuthorizedPickupPerson.photo_url, servable via the app's
    existing /uploads static mount. Lets a gate officer visually confirm
    identity against the photo (see verify_qr_token's reference display)
    and lets this person be issued a printable ID card."""
    person = await db.get(AuthorizedPickupPerson, person_id)
    if not person:
        raise HTTPException(status_code=404, detail="Pickup person not found")
    await assert_parent_of_student(current_user, person.student_id, db)
    await assert_parent_pickup_allowed(current_user, person.student_id, db)
    if current_user.role not in (UserRole.PARENT, *ADMIN_ROLES, UserRole.SECURITY_OFFICER):
        raise HTTPException(status_code=403, detail="Access denied")
    assert_school_access(current_user, person.school_id)

    if file.content_type not in ALLOWED_PHOTO_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {file.content_type}. Allowed: JPEG, PNG.")
    content = await file.read()
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="File is empty.")
    if len(content) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="Photo exceeds the size limit.")

    filename = f"{uuid.uuid4()}_{Path(file.filename or 'photo').name}"
    target_dir = PICKUP_PERSON_PHOTO_DIR / person.school_id / person.id
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / filename).write_bytes(content)

    person.photo_url = f"/uploads/pickup_persons/{person.school_id}/{person.id}/{filename}"
    person.updated_at = datetime.utcnow()
    db.add(person)
    await db.commit()

    return {"message": "Photo uploaded", "photo_url": person.photo_url}


# ── On-Campus Location ───────────────────────────────────────────────────────
# Realistic scope: this codebase has no room-badge/RFID hardware, so "track
# exact location" means staff-updated location state + history, not
# automated real-time positioning — the same honest scoping already applied
# to "verify pickup person identity" above (reference data for a human
# check, not biometric automation).

@router.put("/students/{student_id}/location")
async def update_student_location(
    student_id: str,
    body: UpdateLocationRequest,
    current_user: User = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.TEACHER,
        UserRole.SECURITY_OFFICER, UserRole.NURSE, UserRole.REGISTRAR,
    )),
    _plan_check: User = Depends(require_plan_feature("student_safety")),
    db: AsyncSession = Depends(get_session),
):
    if not body.location or not body.location.strip():
        raise HTTPException(status_code=400, detail="A location is required")

    result = await db.exec(select(StudentSecurityProfile).where(StudentSecurityProfile.student_id == student_id))
    profile = result.first()
    if not profile:
        student = await db.get(Student, student_id)
        if not student:
            raise HTTPException(status_code=404, detail="Student not found")
        profile = StudentSecurityProfile(student_id=student_id, school_id=student.school_id)
        db.add(profile)

    location = body.location.strip()
    now = datetime.utcnow()
    profile.current_location = location
    profile.current_location_updated_at = now
    profile.current_location_updated_by = current_user.id
    profile.updated_at = now
    db.add(profile)

    db.add(StudentLocationLog(
        school_id=profile.school_id, student_id=student_id, location=location, set_by=current_user.id,
    ))
    await db.commit()

    try:
        await broadcaster.publish({
            "type": "student_location_updated",
            "school_id": profile.school_id,
            "student_id": student_id,
            "location": location,
        })
    except Exception:
        logger.exception("Failed to publish student_location_updated event")

    return {"student_id": student_id, "current_location": location, "updated_at": now}


@router.get("/students/{student_id}/location-history", response_model=List[StudentLocationLog])
async def get_student_location_history(
    student_id: str,
    limit: int = Query(50, le=200),
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("student_safety")),
    db: AsyncSession = Depends(get_session),
):
    await assert_parent_of_student(current_user, student_id, db)
    await assert_staff_school_access_for_student(current_user, student_id, db)
    result = await db.exec(
        select(StudentLocationLog)
        .where(StudentLocationLog.student_id == student_id)
        .order_by(StudentLocationLog.created_at.desc())
        .limit(limit)
    )
    return result.all()


@router.get("/location-board")
async def get_location_board(
    school_id: str,
    current_user: User = Depends(require_roles(*OFFICER_ROLES)),
    db: AsyncSession = Depends(get_session),
):
    """Quick school-wide check: every active student with a recorded
    current_location and when it was last updated — for a fast "who's
    where right now" scan, not a live map."""
    assert_school_access(current_user, school_id)
    result = await db.exec(
        select(StudentSecurityProfile).where(
            StudentSecurityProfile.school_id == school_id,
            StudentSecurityProfile.current_location.is_not(None),
        )
    )
    profiles = result.all()
    if not profiles:
        return []

    student_ids = [p.student_id for p in profiles]
    students_result = await db.exec(select(Student).where(Student.id.in_(student_ids)))
    students_by_id = {s.id: s for s in students_result.all()}

    entries = [
        {
            "student_id": p.student_id,
            "student_name": (
                f"{students_by_id[p.student_id].first_name} {students_by_id[p.student_id].last_name}"
                if p.student_id in students_by_id else "Unknown"
            ),
            "location": p.current_location,
            "updated_at": p.current_location_updated_at,
        }
        for p in profiles
    ]
    entries.sort(key=lambda e: e["student_name"])
    return entries


# ── QR Token Generation ───────────────────────────────────────────────────────

@router.post("/qr/generate-daily")
async def generate_daily_qr_tokens(
    body: Optional[GenerateDailyRequest] = None,
    school_id: Optional[str] = None,
    background_tasks: BackgroundTasks = None,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    _plan_check: User = Depends(require_plan_feature("pickup_advanced")),
):
    """
    Generate daily QR tokens for all students with parent pickup method.
    Also emails each generated QR to any linked parent email addresses.
    Call this once per school day — idempotent (skips already-issued tokens).
    """
    if background_tasks is None:
        background_tasks = BackgroundTasks()

    resolved_school_id = (
        (body.school_id if body and getattr(body, "school_id", None) else None)
        or (school_id or "")
    ).strip()
    idempotency_key = (body.idempotency_key if body and getattr(body, 'idempotency_key', None) else None)
    if not resolved_school_id:
        raise HTTPException(status_code=400, detail="school_id is required")
    assert_school_access(current_user, resolved_school_id)

    today = today_str()
    expires = token_expires_at()
    created = 0

    school_result = await db.exec(select(School).where(School.id == resolved_school_id))
    school = school_result.first()
    school_name = school.name if school else "School"

    # Parent pickup tokens — one per student
    today = today_str()
    existing_dispatch = await db.exec(
        select(SchoolQRDispatch).where(
            and_(
                SchoolQRDispatch.school_id == resolved_school_id,
                SchoolQRDispatch.dispatch_date == today,
            )
        )
    )
    found_dispatch = existing_dispatch.first()
    if found_dispatch:
        # Already dispatched for today — return the existing record's metadata
        return {
            "message": f"Dispatch already recorded",
            "date": today,
            "created": 0,
            "sent": True,
            "dispatched_at": found_dispatch.dispatched_at.isoformat() if found_dispatch.dispatched_at else None,
            "idempotency_key": found_dispatch.idempotency_key,
        }

    # Try to create the dispatch record to make the operation idempotent across concurrent requests
    dispatch = SchoolQRDispatch(
        school_id=resolved_school_id,
        dispatch_date=today,
        dispatched_at=datetime.utcnow(),
        dispatched_by=current_user.id if getattr(current_user, 'id', None) else None,
        idempotency_key=idempotency_key or str(uuid.uuid4()),
    )
    db.add(dispatch)
    try:
        await db.commit()
    except IntegrityError:
        # Another request created it concurrently — fetch and return
        await db.rollback()
        existing_dispatch = await db.exec(
            select(SchoolQRDispatch).where(
                and_(
                    SchoolQRDispatch.school_id == resolved_school_id,
                    SchoolQRDispatch.dispatch_date == today,
                )
            )
        )
        found_dispatch = existing_dispatch.first()
        if found_dispatch:
            return {
                "message": f"Dispatch already recorded",
                "date": today,
                "created": 0,
                "sent": True,
                "dispatched_at": found_dispatch.dispatched_at.isoformat() if found_dispatch.dispatched_at else None,
                "idempotency_key": found_dispatch.idempotency_key,
            }
        # If we still don't find it, re-raise
        raise

    # Parent pickup tokens — one per student
    students_result = await db.exec(
        select(StudentSecurityProfile).where(
            and_(
                StudentSecurityProfile.school_id == resolved_school_id,
                StudentSecurityProfile.pickup_method == "parent",
            )
        )
    )
    profiles = students_result.all()

    for profile in profiles:
        existing = await db.exec(
            select(DailyQRToken).where(
                and_(
                    DailyQRToken.student_id == profile.student_id,
                    DailyQRToken.issued_date == today,
                    DailyQRToken.token_type == QRTokenType.PARENT_PICKUP,
                )
            )
        )
        if existing.first():
            continue

        token = DailyQRToken(
            token=str(uuid.uuid4()),
            token_type=QRTokenType.PARENT_PICKUP,
            school_id=resolved_school_id,
            student_id=profile.student_id,
            issued_date=today,
            expires_at=expires,
        )
        db.add(token)
        created += 1

        student_result = await db.exec(select(Student).where(Student.id == profile.student_id))
        student = student_result.first()
        student_name = f"{student.first_name} {student.last_name}" if student else "your child"
        expires_label = expires.strftime("%I:%M %p").lstrip("0")

        parent_emails = await get_parent_emails_for_student(db, profile.student_id)
        for parent_email in parent_emails:
            background_tasks.add_task(
                email_service.send_qr_email,
                to=parent_email,
                student_name=student_name,
                qr_token=token.token,
                expires_at=expires_label,
                school_name=school_name,
            )

        # SMS is the primary channel — parents in this context check texts far
        # more reliably than email. Best-effort, mirrors the email dispatch
        # above: fire-and-forget, no DB logging (matches existing precedent).
        parent_phones = await get_parent_phones_for_student(db, profile.student_id)
        if parent_phones:
            sms_message = (
                f"{student_name}'s pickup QR is ready. View it in your Campusio "
                f"parent portal or check your email. Valid till {expires_label}. - {school_name}"
            )
            background_tasks.add_task(
                sms_service.send_sms,
                phone_numbers=parent_phones,
                message=sms_message,
            )

    await db.commit()
    dispatched_at = dispatch.dispatched_at if getattr(dispatch, 'dispatched_at', None) else datetime.utcnow()
    # Publish SSE event for connected admin clients
    try:
        await broadcaster.publish({
            "type": "qr_dispatch",
            "school_id": resolved_school_id,
            "dispatched_at": dispatched_at.isoformat() if dispatched_at else None,
            "idempotency_key": dispatch.idempotency_key,
            "created": created,
        })
    except Exception:
        logger.exception('Failed to publish qr_dispatch event')
    return {
        "message": f"Generated {created} QR tokens for {today}",
        "date": today,
        "created": created,
        "sent": bool(created),
        "dispatched_at": dispatched_at.isoformat(),
        "idempotency_key": dispatch.idempotency_key,
    }


@router.get("/qr/status")
async def get_qr_status(
    school_id: str,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*OFFICER_ROLES)),
    _plan_check: User = Depends(require_plan_feature("pickup_advanced")),
):
    """Return whether QR tokens were generated for `school_id` today.

    This endpoint is lightweight and used by the frontend to reflect persisted
    dispatch state in the UI.
    """
    assert_school_access(current_user, school_id)
    today = today_str()
    # Prefer explicit dispatch record if present
    dispatch_result = await db.exec(
        select(SchoolQRDispatch).where(
            and_(
                SchoolQRDispatch.school_id == school_id,
                SchoolQRDispatch.dispatch_date == today,
            )
        )
    )
    dispatch = dispatch_result.first()
    if dispatch:
        return {
            "sentToday": True,
            "count": None,
            "dispatched_at": dispatch.dispatched_at.isoformat() if dispatch.dispatched_at else None,
            "idempotency_key": dispatch.idempotency_key,
        }

    result = await db.exec(
        select(DailyQRToken).where(
            and_(
                DailyQRToken.school_id == school_id,
                DailyQRToken.issued_date == today,
                DailyQRToken.token_type == QRTokenType.PARENT_PICKUP,
            )
        )
    )
    tokens = result.all()
    count = len(tokens)
    return {"sentToday": count > 0, "count": count}


STREAM_SUBSCRIBER_ROLES = (
    *OFFICER_ROLES, UserRole.PARENT,
    # Chat participants also subscribe, for new_message pings published by
    # POST /communication/messages (see routers/communication.py).
    UserRole.TEACHER, UserRole.HR, UserRole.NURSE, UserRole.REGISTRAR, UserRole.STOREKEEPER,
    # canteen_face_scan pings published by routers/biometric_adms.py.
    UserRole.CANTEEN_STAFF,
)


@router.get('/events/stream')
async def stream_school_events(
    school_id: str,
    token: str,
    db: AsyncSession = Depends(get_session),
):
    """Server-Sent Events stream for a school: QR dispatch notices and live
    bus-location pushes. (Named generically — was QR-only, now carries more.)

    Auth is via a `token` query param rather than the usual Authorization
    header/`get_current_user` dependency, because browsers' EventSource API
    cannot attach custom headers to its request.

    Parents are included so they get bus_location pushes for their own
    child's route; collector_location is deliberately NOT broadcast here —
    it stays poll-only since it's a person-to-person share scoped to whoever
    holds the (unguessable) session token, not a whole-school-visible resource
    like the bus fleet. Broadcasting it school-wide would let any connected
    parent/officer see another family's live collector-person location
    without ever having to know that token.

    Note: in-process broadcaster only works for single-process deployments.
    """
    from fastapi.responses import StreamingResponse

    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        user_id = payload.get("sub")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")

    user_result = await db.exec(select(User).where(User.id == user_id))
    current_user = user_result.first()
    if not current_user or not current_user.is_active:
        raise HTTPException(status_code=401, detail="Invalid or inactive user")
    if current_user.role not in STREAM_SUBSCRIBER_ROLES:
        raise HTTPException(status_code=403, detail=f"Access denied. Required roles: {[r.value for r in STREAM_SUBSCRIBER_ROLES]}")
    assert_school_access(current_user, school_id)

    async def event_generator():
        q = broadcaster.subscribe()
        try:
            while True:
                try:
                    data = await q.get()
                    # The broadcaster is a single process-wide (and, with Redis,
                    # cross-process) fan-out with no topic/channel concept — every
                    # subscriber's queue receives every published event regardless
                    # of school. Every publisher stamps "school_id" (verified
                    # across all call sites), so filter here at delivery time
                    # rather than deliver-then-trust, or a school-scoped stream
                    # leaks every other school's events (canteen face-scans,
                    # custody-restriction alerts, bus GPS, messages).
                    try:
                        event = json.loads(data)
                    except (TypeError, ValueError):
                        continue
                    if event.get("school_id") != school_id:
                        continue
                    yield f"data: {data}\n\n"
                except asyncio.CancelledError:
                    break
        finally:
            broadcaster.unsubscribe(q)

    return StreamingResponse(event_generator(), media_type='text/event-stream')


@router.get("/qr/student/{student_id}")
async def get_student_qr_token(
    student_id: str,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("pickup_advanced")),
):
    """Get (or lazily create) today's QR token for a student."""
    await assert_parent_of_student(current_user, student_id, db)
    await assert_parent_pickup_allowed(current_user, student_id, db)
    token, student = await get_or_create_qr_token(db, student_id)
    if not token:
        raise HTTPException(status_code=404, detail="Student not found")
    return token


class EmailQRRequest(SQLModel):
    to_email: Optional[str] = None


@router.post("/qr/student/{student_id}/email")
async def email_student_qr(
    student_id: str,
    background_tasks: BackgroundTasks,
    body: Optional[EmailQRRequest] = None,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("pickup_advanced")),
):
    """Email today's pickup QR code — to an explicit pickup-person address if
    given, otherwise to the authenticated parent's own account email."""
    await assert_parent_of_student(current_user, student_id, db)
    await assert_parent_pickup_allowed(current_user, student_id, db)

    recipient = (body.to_email.strip() if body and body.to_email else None) or current_user.email
    if not recipient:
        raise HTTPException(status_code=400, detail="No email address to send to")
    if "@" not in recipient:
        raise HTTPException(status_code=400, detail="Enter a valid email address")

    token, student = await get_or_create_qr_token(db, student_id)
    if not token:
        raise HTTPException(status_code=404, detail="Student not found")

    school_result = await db.exec(select(School).where(School.id == token.school_id))
    school = school_result.first()
    school_name = school.name if school else "School"

    student_name = f"{student.first_name} {student.last_name}" if student else "your child"

    background_tasks.add_task(
        email_service.send_qr_email,
        to=recipient,
        student_name=student_name,
        qr_token=token.token,
        expires_at=token.expires_at.strftime("%I:%M %p").lstrip("0"),
        school_name=school_name,
    )

    return {"message": f"QR code emailed to {recipient}"}


@router.post("/qr/my-children/email")
async def email_all_children_qr(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(UserRole.PARENT)),
    _plan_check: User = Depends(require_plan_feature("pickup_advanced")),
):
    """Email today's pickup QR codes for ALL of the parent's linked children in one message."""
    if not current_user.email:
        raise HTTPException(status_code=400, detail="No email address on your account")

    child_ids = await get_parent_children_ids(current_user, db)
    if not child_ids:
        raise HTTPException(status_code=404, detail="No children linked to your account")

    children_payload = []
    school_id = None
    for student_id in child_ids:
        if await _parent_pickup_restriction(current_user, student_id, db):
            continue  # pickup-restricted for this specific child — skip, don't fail the whole batch
        token, student = await get_or_create_qr_token(db, student_id)
        if not token:
            continue
        school_id = school_id or token.school_id
        children_payload.append({
            "name": f"{student.first_name} {student.last_name}",
            "qr_token": token.token,
            "expires_at": token.expires_at.strftime("%I:%M %p").lstrip("0"),
        })

    if not children_payload:
        raise HTTPException(status_code=404, detail="No QR codes available for your children")

    school_result = await db.exec(select(School).where(School.id == school_id))
    school = school_result.first()
    school_name = school.name if school else "School"

    background_tasks.add_task(
        email_service.send_multi_qr_email,
        to=current_user.email,
        children=children_payload,
        school_name=school_name,
    )

    return {"message": "QR codes emailed successfully", "children_count": len(children_payload)}


@router.post("/qr/transport/{route_id}")
async def generate_transport_qr(
    route_id: str,
    body: SchoolScopedRequest,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*OFFICER_ROLES)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
):
    """Generate (or return existing) dispatch QR for a transport route"""
    school_id = body.school_id
    assert_school_access(current_user, school_id)
    today = today_str()
    result = await db.exec(
        select(DailyQRToken).where(
            and_(
                DailyQRToken.route_id == route_id,
                DailyQRToken.issued_date == today,
                DailyQRToken.token_type == QRTokenType.TRANSPORT_DISPATCH,
            )
        )
    )
    existing = result.first()
    if existing:
        return existing

    token = DailyQRToken(
        token=str(uuid.uuid4()),
        token_type=QRTokenType.TRANSPORT_DISPATCH,
        school_id=school_id,
        route_id=route_id,
        issued_date=today,
        expires_at=token_expires_at(),
    )
    db.add(token)
    await db.commit()
    await db.refresh(token)
    return token


# ── QR Verification ───────────────────────────────────────────────────────────

async def _alert_unauthorized_scan(
    db: AsyncSession, background_tasks: BackgroundTasks, scanned_by: User,
    school_id: str, reason: str, student_id: Optional[str] = None,
) -> None:
    """Best-effort real-time + parent alert on a failed/suspicious QR scan —
    previously this only wrote a passive SecurityScanLog row nobody was
    actively watching. Never raises: an alert failure must not block the
    scan-result response the officer's screen is waiting on."""
    try:
        await broadcaster.publish({
            "type": "unauthorized_scan_attempt",
            "school_id": school_id,
            "student_id": student_id,
            "reason": reason,
            "scanned_by_id": scanned_by.id,
        })
    except Exception:
        logger.exception("Failed to publish unauthorized_scan_attempt event")

    try:
        await security_alert_service.raise_incident(
            db, school_id, "unauthorized_scan",
            sms_message=f"Security alert: an unauthorized pickup QR scan was attempted ({reason}).",
            student_id=student_id, related_user_id=scanned_by.id, details=reason,
        )
    except Exception:
        logger.exception("Failed to raise unauthorized_scan incident")

    if not student_id:
        return
    try:
        student = await db.get(Student, student_id)
        if student:
            await parent_notification_service.notify_parent(
                db, student, scanned_by, background_tasks,
                sms_message=f"Security alert: an unrecognized pickup scan for {student.first_name} {student.last_name} was attempted at school and was NOT authorized. Contact the school if you did not expect this.",
                in_app_subject="Security Alert: Unauthorized Pickup Scan",
                in_app_content=f"An unrecognized pickup scan for {student.first_name} {student.last_name} was attempted and blocked ({reason}). Contact the school if you did not expect this.",
                notification_type="unauthorized_pickup_scan",
                message_type=MessageType.URGENT,
            )
    except Exception:
        logger.exception("Failed to notify parent of unauthorized scan attempt")


@router.post("/qr/verify")
async def verify_qr_token(
    body: VerifyQRRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*OFFICER_ROLES)),
    _plan_check: User = Depends(require_plan_feature("pickup_advanced")),
):
    """
    Verify a scanned QR token.
    On success: marks token as used, creates a CollectorTrackingSession,
    logs the scan, and returns student info + collector session token for
    the officer's screen to display as a second QR.
    """
    token = body.token
    gate_lat = body.gate_lat
    gate_lng = body.gate_lng
    today = today_str()
    now = datetime.utcnow()

    token_result = await db.exec(
        select(DailyQRToken).where(DailyQRToken.token == token)
    )
    qr = token_result.first()

    # Build scan log regardless of outcome
    scan = SecurityScanLog(
        school_id=current_user.school_id or "",
        token_scanned=token,
        scanned_by_id=current_user.id,
        gate_lat=gate_lat,
        gate_lng=gate_lng,
        result=ScanResult.UNAUTHORIZED,
    )

    if not qr:
        scan.result = ScanResult.UNAUTHORIZED
        db.add(scan)
        await db.commit()
        await _alert_unauthorized_scan(db, background_tasks, current_user, current_user.school_id or "", "Token not found")
        return {"result": ScanResult.UNAUTHORIZED, "reason": "Token not found"}

    if current_user.role != UserRole.SUPER_ADMIN and qr.school_id != (current_user.school_id or ""):
        scan.result = ScanResult.UNAUTHORIZED
        db.add(scan)
        await db.commit()
        await _alert_unauthorized_scan(db, background_tasks, current_user, qr.school_id, "Token does not belong to your school", qr.student_id)
        return {"result": ScanResult.UNAUTHORIZED, "reason": "Token does not belong to your school"}

    if qr.issued_date != today or qr.expires_at < now:
        scan.result = ScanResult.EXPIRED
        db.add(scan)
        await db.commit()
        await _alert_unauthorized_scan(db, background_tasks, current_user, qr.school_id, "Token expired", qr.student_id)
        return {"result": ScanResult.EXPIRED, "reason": "Token expired"}

    if qr.is_used:
        scan.result = ScanResult.ALREADY_USED
        db.add(scan)
        await db.commit()
        await _alert_unauthorized_scan(db, background_tasks, current_user, qr.school_id, "Token already used", qr.student_id)
        return {"result": ScanResult.ALREADY_USED, "reason": "Token already used"}

    # Campus-exit hold / lockdown check — BEFORE consuming the token, so a
    # blocked release doesn't burn today's QR code; the parent/collector can
    # retry once an admin lifts the hold. Checked here (not just in
    # update_student_status) so neither guard can be bypassed via QR scan.
    hold_profile = None
    if qr.token_type == QRTokenType.PARENT_PICKUP:
        hold_profile_result = await db.exec(
            select(StudentSecurityProfile).where(StudentSecurityProfile.student_id == qr.student_id)
        )
        hold_profile = hold_profile_result.first()
        if hold_profile:
            try:
                await assert_release_allowed(db, hold_profile)
            except HTTPException as e:
                scan.result = ScanResult.UNAUTHORIZED
                db.add(scan)
                await db.commit()
                await _alert_unauthorized_scan(db, background_tasks, current_user, qr.school_id, e.detail, qr.student_id)
                return {"result": ScanResult.UNAUTHORIZED, "reason": e.detail}

    # Custody-restriction awareness — this codebase has no way to verify
    # WHO is physically presenting a valid code (see collector_name below),
    # so a restricted parent who obtains today's code by any means other
    # than generating it themselves (assert_parent_pickup_allowed only
    # gates issuance/sharing) would otherwise pass through a gate scan with
    # zero system-level warning. We can't safely hard-block here — the
    # legitimate custodial parent uses the exact same per-student code —
    # but we CAN require a named collector and immediately alert the
    # school's own staff so a human can intervene in real time.
    collector_name = (body.collector_name or "").strip() or None

    # An officer picking a specific person from the reference list (instead
    # of typing a free-text name) gets that person's photo_required flag
    # enforced — a newly-added collector nobody at the gate would recognize
    # can be required to have a photo on file before they're ever used.
    if body.person_id and qr.token_type == QRTokenType.PARENT_PICKUP:
        person = await db.get(AuthorizedPickupPerson, body.person_id)
        if not person or person.student_id != qr.student_id or not person.is_active:
            scan.result = ScanResult.UNAUTHORIZED
            db.add(scan)
            await db.commit()
            return {"result": ScanResult.UNAUTHORIZED, "reason": "Selected pickup person not found for this student."}
        if person.photo_required and not person.photo_url:
            scan.result = ScanResult.UNAUTHORIZED
            db.add(scan)
            await db.commit()
            return {
                "result": ScanResult.UNAUTHORIZED,
                "reason": f"{person.name} requires a photo on file before they can be used to collect this student. Upload one first.",
            }
        collector_name = person.name

    has_custody_restriction = False
    if qr.token_type == QRTokenType.PARENT_PICKUP:
        restricted_result = await db.execute(
            select(StudentParent.parent_id).where(
                StudentParent.student_id == qr.student_id,
                StudentParent.is_pickup_restricted == True,  # noqa: E712
            )
        )
        restricted_parent_ids = restricted_result.scalars().all()
        has_custody_restriction = bool(restricted_parent_ids)
        if has_custody_restriction and not collector_name:
            scan.result = ScanResult.UNAUTHORIZED
            db.add(scan)
            await db.commit()
            return {
                "result": ScanResult.UNAUTHORIZED,
                "reason": "This student has a custody restriction on file. Enter the collector's name to proceed — this will be logged and the school's security staff will be alerted.",
                "requires_collector_name": True,
            }

    # AUTHORIZED — mark token used
    qr.is_used = True
    qr.used_at = now
    scan.result = ScanResult.AUTHORIZED
    scan.student_id = qr.student_id

    # Create collector tracking session using the embedded token
    collector_session = None
    if qr.token_type == QRTokenType.PARENT_PICKUP:
        collector_session = CollectorTrackingSession(
            session_token=qr.collector_session_token,
            school_id=qr.school_id,
            student_id=qr.student_id,
            scan_log_id=scan.id,
            collector_name=collector_name,
            gate_lat=gate_lat,
            gate_lng=gate_lng,
        )
        db.add(collector_session)
        scan.collector_session_id = qr.collector_session_token

        # Update student arrival status
        profile_result = await db.exec(
            select(StudentSecurityProfile).where(
                StudentSecurityProfile.student_id == qr.student_id
            )
        )
        profile = profile_result.first()
        if profile:
            reset_profile_if_stale(profile, now)
            # Capture the queue count BEFORE mutating this profile's own
            # status below, so it can't be double-counted by an autoflush
            # of the pending change happening in between.
            waiting_count = (await db.execute(
                select(func.count(StudentSecurityProfile.id)).where(
                    StudentSecurityProfile.school_id == qr.school_id,
                    StudentSecurityProfile.arrival_status == ArrivalStatus.EN_ROUTE_COLLECTOR,
                )
            )).scalar()

            profile.arrival_status = ArrivalStatus.EN_ROUTE_COLLECTOR
            # The QR scan IS the verified "parent physically arrived at the
            # gate" moment — assign a queue position (today's count of
            # already-waiting profiles + 1) so staff can dismiss in arrival
            # order instead of one scan at a time with no ordering.
            profile.parent_arrived_at = now
            profile.parent_on_the_way = False
            profile.parent_on_the_way_at = None
            profile.parent_eta_minutes = None
            profile.queue_position = (waiting_count or 0) + 1
            profile.updated_at = now
            db.add(profile)

        # Log arrival event
        event = ArrivalEvent(
            school_id=qr.school_id,
            student_id=qr.student_id,
            event_type=ArrivalEventType.EN_ROUTE_COLLECTOR,
            triggered_by_id=current_user.id,
            notes=f"Released at gate by {current_user.first_name} {current_user.last_name}",
        )
        db.add(event)

    db.add(qr)
    db.add(scan)
    await db.commit()

    if qr.token_type == QRTokenType.PARENT_PICKUP:
        try:
            await broadcaster.publish({
                "type": "pickup_queue_updated",
                "school_id": qr.school_id,
                "student_id": qr.student_id,
            })
        except Exception:
            logger.exception("Failed to publish pickup_queue_updated event")

        if has_custody_restriction:
            try:
                await security_alert_service.raise_incident(
                    db, qr.school_id, "custody_restriction_active_scan",
                    sms_message=f"Security alert: a pickup scan was authorized for a student with an active custody restriction. Collector recorded as: {collector_name}.",
                    student_id=qr.student_id, related_user_id=current_user.id,
                    details=f"Collector name given at scan: {collector_name}",
                )
            except Exception:
                logger.exception("Failed to raise custody_restriction_active_scan incident")

    # Fetch student info for the response
    student_result = await db.exec(
        select(Student).where(Student.id == qr.student_id)
    )
    student = student_result.first()

    # Surface the authorized-pickup-person reference list (with photos)
    # right on the scan response — previously the officer had to separately
    # navigate to GET /students/{id}/pickup-persons on another screen to
    # cross-check identity, so nothing pushed the reference data at the
    # moment it actually matters.
    pickup_persons_payload = []
    if qr.token_type == QRTokenType.PARENT_PICKUP:
        persons_result = await db.exec(
            select(AuthorizedPickupPerson).where(
                AuthorizedPickupPerson.student_id == qr.student_id,
                AuthorizedPickupPerson.is_active == True,  # noqa: E712
            )
        )
        active_persons = persons_result.all()
        card_statuses = await get_pickup_person_card_statuses(db, [p.id for p in active_persons])
        pickup_persons_payload = [
            {
                "name": p.name, "relationship": p.relationship, "phone": p.phone,
                "photo_url": p.photo_url, "notes": p.notes, "card_status": card_statuses.get(p.id),
            }
            for p in active_persons
        ]

    return {
        "result": ScanResult.AUTHORIZED,
        "token_type": qr.token_type,
        "student": {
            "id": student.id if student else qr.student_id,
            "name": f"{student.first_name} {student.last_name}" if student else "Unknown",
        },
        "collector_session_token": qr.collector_session_token if qr.token_type == QRTokenType.PARENT_PICKUP else None,
        "scan_id": scan.id,
        "collector_name": collector_name,
        "has_custody_restriction": has_custody_restriction,
        "authorized_pickup_persons": pickup_persons_payload,
    }


# ── Arrival Status ────────────────────────────────────────────────────────────

@router.get("/students")
async def get_all_students_status(
    school_id: str,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*OFFICER_ROLES)),
):
    """All students in the school with their security status.
    Auto-creates a default profile for any student that doesn't have one yet."""
    assert_school_access(current_user, school_id)

    # Fetch all students in the school
    all_students_result = await db.exec(
        select(Student).where(Student.school_id == school_id)
    )
    all_students = all_students_result.all()

    if not all_students:
        return []

    student_ids = [s.id for s in all_students]

    # Fetch existing profiles
    profiles_result = await db.exec(
        select(StudentSecurityProfile).where(
            StudentSecurityProfile.student_id.in_(student_ids)
        )
    )
    profiles_by_student = {p.student_id: p for p in profiles_result.all()}

    # Auto-create missing profiles in bulk then re-fetch to avoid SQLAlchemy expiry
    missing = [s for s in all_students if s.id not in profiles_by_student]
    if missing:
        for student in missing:
            db.add(StudentSecurityProfile(student_id=student.id, school_id=school_id))
        await db.commit()
        # Re-fetch both students and profiles — commit() expires all loaded objects
        all_students_result2 = await db.exec(
            select(Student).where(Student.school_id == school_id)
        )
        all_students = all_students_result2.all()
        profiles_result2 = await db.exec(
            select(StudentSecurityProfile).where(
                StudentSecurityProfile.student_id.in_(student_ids)
            )
        )
        profiles_by_student = {p.student_id: p for p in profiles_result2.all()}

    # Roll back statuses left over from a prior school day
    now = datetime.utcnow()
    stale_reset = False
    for profile in profiles_by_student.values():
        if reset_profile_if_stale(profile, now):
            db.add(profile)
            stale_reset = True
    if stale_reset:
        await db.commit()

    # Resolve class names in one query
    class_ids = {s.class_id for s in all_students if s.class_id}
    class_names: dict[str, str] = {}
    if class_ids:
        classes_result = await db.exec(select(Class).where(Class.id.in_(class_ids)))
        class_names = {c.id: c.name for c in classes_result.all()}

    # Fetch all notes for these students in one query
    notes_result = await db.exec(
        select(ParentNote).where(
            ParentNote.student_id.in_(student_ids)
        ).order_by(ParentNote.created_at.desc())
    )
    notes_by_student: dict[str, list] = {}
    for note in notes_result.all():
        notes_by_student.setdefault(note.student_id, []).append(note)

    restricted_ids = await get_restricted_student_ids(db, student_ids)

    students = []
    for student in all_students:
        profile = profiles_by_student[student.id]
        notes = notes_by_student.get(student.id, [])
        students.append({
            "id": student.id,
            "name": f"{student.first_name} {student.last_name}",
            "class": class_names.get(student.class_id, ""),
            "gender": student.gender,
            "pickup_method": profile.pickup_method,
            "transport_route_id": profile.transport_route_id,
            "ghana_post_address": profile.ghana_post_address,
            "neighbourhood": profile.neighbourhood,
            "home_lat": profile.home_lat,
            "home_lng": profile.home_lng,
            "arrival_status": profile.arrival_status,
            "arrival_time": profile.arrival_time,
            "confirmed_at": profile.confirmed_at,
            "release_hold": profile.release_hold,
            "release_hold_reason": profile.release_hold_reason,
            "has_custody_restriction": student.id in restricted_ids,
            "current_location": profile.current_location,
            "current_location_updated_at": profile.current_location_updated_at,
            "parent_notes": [
                {
                    "id": n.id,
                    "text": n.text,
                    "author_id": n.author_id,
                    "is_confirmation": n.is_confirmation,
                    "created_at": n.created_at,
                }
                for n in notes
            ],
        })

    return students


@router.patch("/students/{student_id}/status")
async def update_student_status(
    student_id: str,
    body: UpdateArrivalStatusRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN,
        UserRole.SECURITY_OFFICER, UserRole.DRIVER
    )),
    _plan_check: User = Depends(require_plan_feature("student_safety")),
):
    """Update a student's arrival status — used by officers and drivers"""
    arrival_status = body.arrival_status
    result = await db.exec(
        select(StudentSecurityProfile).where(
            StudentSecurityProfile.student_id == student_id
        )
    )
    profile = result.first()
    if not profile:
        raise HTTPException(status_code=404, detail="Security profile not found")
    assert_school_access(current_user, profile.school_id)
    await assert_release_allowed(db, profile)

    now = datetime.utcnow()
    profile.arrival_status = arrival_status
    profile.updated_at = now

    if arrival_status == ArrivalStatus.ARRIVED_UNCONFIRMED:
        profile.arrival_time = now

    db.add(profile)

    event_map = {
        ArrivalStatus.EN_ROUTE_BUS: ArrivalEventType.EN_ROUTE_BUS,
        ArrivalStatus.EN_ROUTE_COLLECTOR: ArrivalEventType.EN_ROUTE_COLLECTOR,
        ArrivalStatus.ARRIVED_UNCONFIRMED: ArrivalEventType.ARRIVED,
        ArrivalStatus.SAFE_CONFIRMED: ArrivalEventType.PARENT_CONFIRMED,
    }
    if arrival_status in event_map:
        event = ArrivalEvent(
            school_id=profile.school_id,
            student_id=student_id,
            event_type=event_map[arrival_status],
            triggered_by_id=current_user.id,
        )
        db.add(event)

    await db.commit()
    await db.refresh(profile)

    # Staff (not the parent's own confirm_safe_arrival below) marking a pickup
    # complete is also this school's gate checkout, for late-pickup reporting
    # (models/gate_attendance.py) — best-effort, never blocks the status update.
    if arrival_status == ArrivalStatus.SAFE_CONFIRMED:
        try:
            student = await db.get(Student, student_id)
            if student:
                row, is_late_pickup = await gate_attendance_service.record_check_out(
                    db, profile.school_id, student_id, recorded_by=current_user.id, method="qr_pickup",
                )
                time_label = row.check_out_time.strftime("%H:%M")
                await parent_notification_service.notify_parent(
                    db, student, current_user, background_tasks,
                    sms_message=(
                        f"{student.first_name} {student.last_name} was picked up from school at {time_label}"
                        + (" (late pickup)." if is_late_pickup else ".")
                    ),
                    in_app_subject="School Check-Out" + (" (Late Pickup)" if is_late_pickup else ""),
                    in_app_content=f"{student.first_name} {student.last_name} was collected from school at {time_label}"
                                    + (" — this was after the school's pickup time." if is_late_pickup else "."),
                    notification_type="gate_check_out",
                    message_type=MessageType.ATTENDANCE,
                )
        except Exception:
            logger.exception("Failed to record gate checkout / notify parent for QR pickup completion")

    return {"status": profile.arrival_status, "updated_at": profile.updated_at}


@router.post("/students/{student_id}/confirm")
async def parent_confirm_safe_arrival(
    student_id: str,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(UserRole.PARENT)),
):
    """Parent confirms their child arrived home safely"""
    await assert_parent_of_student(current_user, student_id, db)
    result = await db.exec(
        select(StudentSecurityProfile).where(
            StudentSecurityProfile.student_id == student_id
        )
    )
    profile = result.first()
    if not profile:
        raise HTTPException(status_code=404, detail="Security profile not found")
    # Previously missing here (unlike every other release path) — a held
    # student could be self-confirmed "safe" by their own parent while
    # release_hold was still active, a direct state contradiction for a
    # case that exists specifically because someone judged the student
    # unsafe to release.
    await assert_release_allowed(db, profile)

    now = datetime.utcnow()
    profile.arrival_status = ArrivalStatus.SAFE_CONFIRMED
    profile.confirmed_at = now
    profile.updated_at = now
    db.add(profile)

    # This is the OTHER path to SAFE_CONFIRMED besides staff dismissal
    # (update_student_status) — previously only that staff path wrote to
    # the official GateAttendance ledger, so a school relying on parents to
    # self-confirm could go a whole day with no check-out record at all for
    # late-pickup reporting (routers/gate_attendance.py::list_late_pickups).
    try:
        await gate_attendance_service.record_check_out(
            db, profile.school_id, student_id, recorded_by=current_user.id, method="parent_confirm",
        )
    except Exception:
        logger.exception("Failed to record gate checkout for parent self-confirmation")

    # Auto-create a confirmation note
    note = ParentNote(
        school_id=profile.school_id,
        student_id=student_id,
        author_id=current_user.id,
        text=f"Confirmed safe arrival at {now.strftime('%H:%M')}.",
        is_confirmation=True,
    )
    db.add(note)

    # Log event
    event = ArrivalEvent(
        school_id=profile.school_id,
        student_id=student_id,
        event_type=ArrivalEventType.PARENT_CONFIRMED,
        triggered_by_id=current_user.id,
    )
    db.add(event)

    # Close any active collector session for this student
    session_result = await db.exec(
        select(CollectorTrackingSession).where(
            and_(
                CollectorTrackingSession.student_id == student_id,
                CollectorTrackingSession.is_active == True,
            )
        )
    )
    session = session_result.first()
    if session:
        session.is_active = False
        session.ended_at = now
        db.add(session)

    await db.commit()
    return {"status": "safe_confirmed", "confirmed_at": now}


# ── "On My Way" — advance parent-initiated pickup notice ───────────────────────

@router.post("/students/{student_id}/on-my-way")
async def parent_on_my_way(
    student_id: str,
    body: OnMyWayRequest,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(UserRole.PARENT)),
    _plan_check: User = Depends(require_plan_feature("student_safety")),
):
    """A parent signals they're heading to school to collect their child,
    before physically arriving/scanning a QR code — distinct from
    verify_qr_token (requires being at the gate) and update_student_status
    (staff-only). Lets gate staff get the child ready ahead of time instead
    of only reacting once someone scans in."""
    await assert_parent_of_student(current_user, student_id, db)
    await assert_parent_pickup_allowed(current_user, student_id, db)
    result = await db.exec(select(StudentSecurityProfile).where(StudentSecurityProfile.student_id == student_id))
    profile = result.first()
    if not profile:
        raise HTTPException(status_code=404, detail="Security profile not found")
    await assert_release_allowed(db, profile)

    now = datetime.utcnow()
    reset_profile_if_stale(profile, now)
    profile.parent_on_the_way = True
    profile.parent_on_the_way_at = now
    profile.parent_eta_minutes = body.eta_minutes
    profile.updated_at = now
    db.add(profile)
    await db.commit()

    try:
        await broadcaster.publish({
            "type": "parent_on_the_way",
            "school_id": profile.school_id,
            "student_id": student_id,
            "eta_minutes": body.eta_minutes,
        })
    except Exception:
        logger.exception("Failed to publish parent_on_the_way event")

    return {"student_id": student_id, "parent_on_the_way": True, "eta_minutes": body.eta_minutes}


@router.delete("/students/{student_id}/on-my-way")
async def cancel_on_my_way(
    student_id: str,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(UserRole.PARENT)),
    _plan_check: User = Depends(require_plan_feature("student_safety")),
):
    """Cancel an "on my way" signal (e.g. a mis-tap, or plans changed)."""
    await assert_parent_of_student(current_user, student_id, db)
    result = await db.exec(select(StudentSecurityProfile).where(StudentSecurityProfile.student_id == student_id))
    profile = result.first()
    if not profile:
        raise HTTPException(status_code=404, detail="Security profile not found")

    profile.parent_on_the_way = False
    profile.parent_on_the_way_at = None
    profile.parent_eta_minutes = None
    profile.updated_at = datetime.utcnow()
    db.add(profile)
    await db.commit()
    return {"student_id": student_id, "parent_on_the_way": False}


@router.get("/pickup-queue")
async def get_pickup_queue(
    school_id: str,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*OFFICER_ROLES)),
    _plan_check: User = Depends(require_plan_feature("student_safety")),
):
    """Drive-through staging view for gate staff: who has signaled they're
    on their way (advance notice, not yet arrived) and who has physically
    arrived and verified (via QR scan) and is waiting to be dismissed, in
    arrival order — so staff can prepare students ahead of time and dismiss
    in a predictable order instead of reacting to one scan at a time with
    no visibility into who else is waiting."""
    assert_school_access(current_user, school_id)
    now = datetime.utcnow()

    profiles_result = await db.exec(
        select(StudentSecurityProfile).where(StudentSecurityProfile.school_id == school_id)
    )
    profiles = profiles_result.all()

    stale_reset = False
    for profile in profiles:
        if reset_profile_if_stale(profile, now):
            db.add(profile)
            stale_reset = True
    if stale_reset:
        await db.commit()

    on_the_way = [p for p in profiles if p.parent_on_the_way and p.arrival_status == ArrivalStatus.PENDING]
    waiting = sorted(
        (p for p in profiles if p.arrival_status == ArrivalStatus.EN_ROUTE_COLLECTOR),
        key=lambda p: p.queue_position or 0,
    )

    student_ids = [p.student_id for p in on_the_way] + [p.student_id for p in waiting]
    students_by_id = {}
    if student_ids:
        students_result = await db.exec(select(Student).where(Student.id.in_(student_ids)))
        students_by_id = {s.id: s for s in students_result.all()}

    def _student_name(student_id: str) -> str:
        s = students_by_id.get(student_id)
        return f"{s.first_name} {s.last_name}" if s else "Unknown"

    restricted_ids = await get_restricted_student_ids(db, student_ids)

    return {
        "on_the_way": [
            {
                "student_id": p.student_id,
                "student_name": _student_name(p.student_id),
                "eta_minutes": p.parent_eta_minutes,
                "signaled_at": p.parent_on_the_way_at,
                "release_hold": p.release_hold,
                "has_custody_restriction": p.student_id in restricted_ids,
            }
            for p in on_the_way
        ],
        "waiting": [
            {
                "student_id": p.student_id,
                "student_name": _student_name(p.student_id),
                "queue_position": p.queue_position,
                "arrived_at": p.parent_arrived_at,
                # Previously only visible on the full roster
                # (get_all_students_status/get_admin_overview) — this is the
                # screen staff actually watch during the pickup rush, so
                # discovery was happening only at scan-rejection time.
                "release_hold": p.release_hold,
                "has_custody_restriction": p.student_id in restricted_ids,
            }
            for p in waiting
        ],
    }


# ── Parent-accessible child status ───────────────────────────────────────────

@router.get("/students/{student_id}/status")
async def get_student_security_status(
    student_id: str,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Returns security profile + active collector/bus session for ONE student.
    Parents use this for their own children; officers/admins can use it for any student.
    """
    await assert_parent_of_student(current_user, student_id, db)
    await assert_staff_school_access_for_student(current_user, student_id, db)
    profile_result = await db.exec(
        select(StudentSecurityProfile).where(
            StudentSecurityProfile.student_id == student_id
        )
    )
    profile = profile_result.first()
    if not profile:
        # Auto-create a default profile so the parent portal never errors on first load
        student_result = await db.exec(select(Student).where(Student.id == student_id))
        student = student_result.first()
        if not student:
            raise HTTPException(status_code=404, detail="Student not found")
        profile = StudentSecurityProfile(
            student_id=student_id,
            school_id=str(student.school_id),
        )
        db.add(profile)
        await db.commit()
        await db.refresh(profile)

    notes_result = await db.exec(
        select(ParentNote).where(
            ParentNote.student_id == student_id
        ).order_by(ParentNote.created_at.desc())
    )
    notes = notes_result.all()

    collector_result = await db.exec(
        select(CollectorTrackingSession).where(
            and_(
                CollectorTrackingSession.student_id == student_id,
                CollectorTrackingSession.is_active == True,
            )
        )
    )
    collector = collector_result.first()

    return {
        "student_id": student_id,
        "pickup_method": profile.pickup_method,
        "transport_route_id": profile.transport_route_id,
        "ghana_post_address": profile.ghana_post_address,
        "neighbourhood": profile.neighbourhood,
        "home_lat": profile.home_lat,
        "home_lng": profile.home_lng,
        "arrival_status": profile.arrival_status,
        "arrival_time": profile.arrival_time,
        "confirmed_at": profile.confirmed_at,
        "parent_on_the_way": profile.parent_on_the_way,
        "parent_on_the_way_at": profile.parent_on_the_way_at,
        "parent_eta_minutes": profile.parent_eta_minutes,
        "parent_arrived_at": profile.parent_arrived_at,
        "queue_position": profile.queue_position,
        "release_hold": profile.release_hold,
        "current_location": profile.current_location,
        "current_location_updated_at": profile.current_location_updated_at,
        "collector_session_token": collector.session_token if collector else None,
        "notes": [
            {
                "id": n.id,
                "text": n.text,
                "author_id": n.author_id,
                "is_confirmation": n.is_confirmation,
                "created_at": str(n.created_at),
            }
            for n in notes
        ],
    }


# ── Parent Notes ──────────────────────────────────────────────────────────────

@router.get("/students/{student_id}/notes")
async def get_student_notes(
    student_id: str,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    await assert_parent_of_student(current_user, student_id, db)
    await assert_staff_school_access_for_student(current_user, student_id, db)
    result = await db.exec(
        select(ParentNote).where(
            ParentNote.student_id == student_id
        ).order_by(ParentNote.created_at.desc())
    )
    return result.all()


@router.post("/students/{student_id}/notes")
async def add_parent_note(
    student_id: str,
    data: ParentNoteCreate,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(UserRole.PARENT)),
):
    await assert_parent_of_student(current_user, student_id, db)
    profile_result = await db.exec(
        select(StudentSecurityProfile).where(
            StudentSecurityProfile.student_id == student_id
        )
    )
    profile = profile_result.first()
    if not profile:
        raise HTTPException(status_code=404, detail="Security profile not found")

    note = ParentNote(
        school_id=profile.school_id,
        student_id=student_id,
        author_id=current_user.id,
        text=data.text,
        is_confirmation=data.is_confirmation,
    )
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return note


# ── Transport Dispatch ────────────────────────────────────────────────────────

@router.post("/transport/{route_id}/dispatch")
async def dispatch_route(
    route_id: str,
    body: DispatchRouteRequest,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*OFFICER_ROLES)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
):
    """
    Dispatch a transport route — sets boarded students to en_route_bus and
    logs departure events. Pass boarded_student_ids for a real headcount
    (only those students are marked en route; anyone enrolled but not in
    the list is logged as not-boarded and left in their current status, so
    the security screen doesn't show an absent/held-back student as "en
    route" on trust alone). Omit it to keep the previous behavior of
    marking every enrolled student.
    """
    school_id = body.school_id
    assert_school_access(current_user, school_id)
    await assert_no_pickup_lockdown(db, school_id)
    result = await db.exec(
        select(StudentSecurityProfile).where(
            and_(
                StudentSecurityProfile.school_id == school_id,
                StudentSecurityProfile.transport_route_id == route_id,
                StudentSecurityProfile.pickup_method == "transport",
            )
        )
    )
    profiles = result.all()

    if not profiles:
        raise HTTPException(status_code=404, detail="No students found on this route")

    boarded_ids = set(body.boarded_student_ids) if body.boarded_student_ids is not None else None

    now = datetime.utcnow()
    dispatched = 0
    not_boarded = 0
    for profile in profiles:
        if boarded_ids is not None and profile.student_id not in boarded_ids:
            db.add(ArrivalEvent(
                school_id=school_id,
                student_id=profile.student_id,
                event_type=ArrivalEventType.DISPATCHED,
                triggered_by_id=current_user.id,
                notes=f"Route {route_id} dispatched — student not marked as boarded, status left unchanged",
            ))
            not_boarded += 1
            continue

        profile.arrival_status = ArrivalStatus.EN_ROUTE_BUS
        profile.updated_at = now
        db.add(profile)

        event = ArrivalEvent(
            school_id=school_id,
            student_id=profile.student_id,
            event_type=ArrivalEventType.EN_ROUTE_BUS,
            triggered_by_id=current_user.id,
            notes=f"Route {route_id} dispatched",
        )
        db.add(event)
        dispatched += 1

    await db.commit()
    return {"dispatched": dispatched, "not_boarded": not_boarded, "route_id": route_id, "dispatched_at": now}


async def _mark_route_students_arrived(
    db: AsyncSession, school_id: str, route_id: str, triggered_by_id: str,
    arrived_student_ids: Optional[set] = None, note: str = "",
) -> int:
    """Shared by the manual bulk-arrival endpoint below and post_bus_location's
    geofence auto-trigger. Only ever touches students currently EN_ROUTE_BUS
    on this route — that's what makes the geofence trigger idempotent
    across repeated GPS pings inside the geofence (once moved to
    ARRIVED_UNCONFIRMED, a student no longer matches this filter, so a
    later ping is a no-op instead of re-firing every 10 seconds)."""
    result = await db.exec(
        select(StudentSecurityProfile).where(
            StudentSecurityProfile.school_id == school_id,
            StudentSecurityProfile.transport_route_id == route_id,
            StudentSecurityProfile.arrival_status == ArrivalStatus.EN_ROUTE_BUS,
        )
    )
    profiles = result.all()
    now = datetime.utcnow()
    marked = 0
    for profile in profiles:
        if arrived_student_ids is not None and profile.student_id not in arrived_student_ids:
            continue
        profile.arrival_status = ArrivalStatus.ARRIVED_UNCONFIRMED
        profile.arrival_time = now
        profile.updated_at = now
        db.add(profile)
        db.add(ArrivalEvent(
            school_id=school_id, student_id=profile.student_id, event_type=ArrivalEventType.ARRIVED,
            triggered_by_id=triggered_by_id, notes=note or f"Route {route_id} arrived",
        ))
        marked += 1
    if marked:
        await db.commit()
    return marked


@router.post("/transport/{route_id}/arrived")
async def mark_route_arrived(
    route_id: str,
    body: DispatchRouteRequest,  # reuses school_id + an optional student subset, same shape as dispatch
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*OFFICER_ROLES)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
):
    """Bulk-closes the other end of dispatch_route — previously the only
    way to move a bus-riding student past EN_ROUTE_BUS was one PATCH
    /students/{id}/status per student, once staff noticed the bus had
    arrived. Pass boarded_student_ids (reusing DispatchRouteRequest's field
    as "arrived student ids" here) to mark only a subset; omit it to mark
    everyone still EN_ROUTE_BUS on this route. A route with a configured
    arrival geofence (Route.destination_lat/lng/arrival_geofence_meters)
    also triggers this automatically from post_bus_location — this endpoint
    remains for routes without one configured, or a manual override."""
    school_id = body.school_id
    assert_school_access(current_user, school_id)
    arrived_ids = set(body.boarded_student_ids) if body.boarded_student_ids is not None else None
    marked = await _mark_route_students_arrived(
        db, school_id, route_id, current_user.id, arrived_student_ids=arrived_ids,
        note=f"Route {route_id} arrived (marked by {current_user.first_name} {current_user.last_name})",
    )
    return {"marked_arrived": marked, "route_id": route_id}


class ReportTransportIncidentRequest(SchoolScopedRequest):
    incident_type: str  # "breakdown" | "delay" | "accident" | "other"
    description: str


@router.post("/transport/{route_id}/incident")
async def report_transport_incident(
    route_id: str,
    body: ReportTransportIncidentRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(UserRole.DRIVER, *OFFICER_ROLES)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
):
    """A driver/officer reports a breakdown, major delay, or accident —
    previously nothing in this codebase handled "the bus isn't coming /
    isn't on time" as an event at all: no parent notification, no
    alternate-arrangement trigger. This raises a persisted, admin-alerting
    SecurityIncident AND notifies every parent with a child currently
    enrolled on this route, so a family knows to make other arrangements
    instead of finding out only when the bus never shows up."""
    school_id = body.school_id
    assert_school_access(current_user, school_id)
    if not (body.description or "").strip():
        raise HTTPException(status_code=400, detail="A description is required")

    route = await db.get(TransportRoute, route_id)
    if not route or route.school_id != school_id:
        raise HTTPException(status_code=404, detail="Route not found")

    incident = await security_alert_service.raise_incident(
        db, school_id, f"transport_{body.incident_type}",
        sms_message=f"Transport alert: route '{route.route_name}' reported {body.incident_type} — {body.description.strip()}",
        details=f"route_id:{route_id} {body.description.strip()}",
        related_user_id=current_user.id,
    )

    profiles_result = await db.exec(
        select(StudentSecurityProfile).where(
            StudentSecurityProfile.school_id == school_id,
            StudentSecurityProfile.transport_route_id == route_id,
        )
    )
    profiles = profiles_result.all()
    notified = 0
    for profile in profiles:
        student = await db.get(Student, profile.student_id)
        if not student:
            continue
        await parent_notification_service.notify_parent(
            db, student, current_user, background_tasks,
            sms_message=f"Transport alert for {student.first_name} {student.last_name}'s route ({route.route_name}): {body.incident_type} — {body.description.strip()}",
            in_app_subject=f"Transport Alert: {body.incident_type.title()}",
            in_app_content=f"Route '{route.route_name}' reported {body.incident_type}: {body.description.strip()}",
            notification_type="transport_incident",
            message_type=MessageType.URGENT,
        )
        notified += 1

    return {"incident_id": incident.id, "route_id": route_id, "parents_notified": notified}


# ── Live Bus Location ─────────────────────────────────────────────────────────

async def resolve_driver_staff_id(current_user: User, route: "TransportRoute", db: AsyncSession) -> Optional[str]:
    """LiveBusLocation.driver_id is FK'd to driver_staff.id, not users.id — found
    live when the very first real driver account tried to post a location and
    hit a ForeignKeyViolationError (nothing had ever exercised this insert
    before, since no DRIVER-role user existed in this data set until now).

    For a DRIVER caller, resolve their own User -> Staff -> DriverStaff chain
    (guaranteed to exist here, since get_driver_route_ids already validated it
    to let them reach this endpoint at all). For an admin override-posting on
    a route's behalf, attribute the position to the route's vehicle's actual
    assigned driver instead, since admins have no Staff/DriverStaff record of
    their own to satisfy the FK.
    """
    if current_user.role == UserRole.DRIVER:
        staff_result = await db.exec(select(Staff).where(Staff.user_id == current_user.id))
        staff = staff_result.first()
        if not staff:
            return None
        driver_staff_result = await db.exec(select(DriverStaff).where(DriverStaff.staff_id == staff.id))
        driver_staff = driver_staff_result.first()
        return driver_staff.id if driver_staff else None

    if not route.vehicle_id:
        return None
    vehicle_result = await db.exec(select(Vehicle).where(Vehicle.id == route.vehicle_id))
    vehicle = vehicle_result.first()
    return vehicle.driver_id if vehicle else None


async def is_transport_session_active(db: AsyncSession, route_id: str) -> bool:
    """A route's tracking session is active once today's TRANSPORT_DISPATCH
    token has been marked used via the /activate endpoint below."""
    result = await db.exec(
        select(DailyQRToken).where(
            and_(
                DailyQRToken.route_id == route_id,
                DailyQRToken.issued_date == today_str(),
                DailyQRToken.token_type == QRTokenType.TRANSPORT_DISPATCH,
                DailyQRToken.is_used == True,
            )
        )
    )
    return result.first() is not None


@router.post("/qr/transport/{route_id}/activate")
async def activate_transport_session(
    route_id: str,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(UserRole.DRIVER, *ADMIN_ROLES)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
):
    """Start today's live-tracking session for a route.

    The driver's tracking page calls this on load (or when the admin's
    driver-QR deep link opens it) before GPS posting is allowed. Idempotent —
    re-activating an already-active session for today just confirms it, so a
    page refresh or reopening the link doesn't error. The session's natural
    end is midnight (same expiry as pickup tokens); there's no separate
    "deactivate" call.
    """
    route_result = await db.exec(select(TransportRoute).where(TransportRoute.id == route_id))
    route = route_result.first()
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")
    assert_school_access(current_user, route.school_id)

    if current_user.role == UserRole.DRIVER:
        driver_route_ids = await get_driver_route_ids(current_user, db)
        if route_id not in (driver_route_ids or []):
            raise HTTPException(status_code=403, detail="You are not the assigned driver for this route")

    today = today_str()
    result = await db.exec(
        select(DailyQRToken).where(
            and_(
                DailyQRToken.route_id == route_id,
                DailyQRToken.issued_date == today,
                DailyQRToken.token_type == QRTokenType.TRANSPORT_DISPATCH,
            )
        )
    )
    token = result.first()
    if not token:
        token = DailyQRToken(
            token=str(uuid.uuid4()),
            token_type=QRTokenType.TRANSPORT_DISPATCH,
            school_id=route.school_id,
            route_id=route_id,
            issued_date=today,
            expires_at=token_expires_at(),
        )

    token.is_used = True
    token.used_at = datetime.utcnow()
    db.add(token)
    await db.commit()
    await db.refresh(token)
    return {"active": True, "expires_at": token.expires_at}


@router.post("/transport/{route_id}/location")
async def post_bus_location(
    route_id: str,
    data: LiveBusLocationCreate,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(UserRole.DRIVER, *ADMIN_ROLES)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
):
    """Driver posts current GPS coordinates — called every 10s from driver's device"""
    route_result = await db.exec(select(TransportRoute).where(TransportRoute.id == route_id))
    route = route_result.first()
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")
    assert_school_access(current_user, route.school_id)

    if current_user.role == UserRole.DRIVER:
        driver_route_ids = await get_driver_route_ids(current_user, db)
        if route_id not in (driver_route_ids or []):
            raise HTTPException(status_code=403, detail="You are not the assigned driver for this route")

    if not await is_transport_session_active(db, route_id):
        raise HTTPException(
            status_code=403,
            detail="Tracking session not activated for this route today. Open the driver page to activate it.",
        )

    driver_staff_id = await resolve_driver_staff_id(current_user, route, db)
    if not driver_staff_id:
        raise HTTPException(
            status_code=400,
            detail="No driver record on file for this route's vehicle — assign a driver before posting location.",
        )

    loc = LiveBusLocation(
        school_id=route.school_id,
        route_id=route_id,
        driver_id=driver_staff_id,
        lat=data.lat,
        lng=data.lng,
        speed_kmh=data.speed_kmh,
        heading=data.heading,
    )
    db.add(loc)
    await db.commit()

    try:
        await broadcaster.publish({
            "type": "bus_location",
            "school_id": route.school_id,
            "route_id": route_id,
            "lat": data.lat,
            "lng": data.lng,
            "speed_kmh": data.speed_kmh,
            "heading": data.heading,
            "recorded_at": loc.recorded_at.isoformat() if loc.recorded_at else None,
        })
    except Exception:
        logger.exception('Failed to publish bus_location event')

    # Speed alert — debounced against the same route's own recent alerts
    # (route_id is embedded in `details`, matched below) so a sustained
    # speeding event doesn't fire an SMS on every ~10s ping.
    if route.max_speed_kmh is not None and data.speed_kmh is not None and data.speed_kmh > route.max_speed_kmh:
        SPEED_ALERT_DEBOUNCE_MINUTES = 10
        cutoff = datetime.utcnow() - timedelta(minutes=SPEED_ALERT_DEBOUNCE_MINUTES)
        recent = await db.execute(
            select(SecurityIncident.id).where(
                SecurityIncident.school_id == route.school_id,
                SecurityIncident.incident_type == "transport_speeding",
                SecurityIncident.details.contains(f"route_id:{route_id}"),
                SecurityIncident.created_at >= cutoff,
            ).limit(1)
        )
        if recent.scalar_one_or_none() is None:
            try:
                await security_alert_service.raise_incident(
                    db, route.school_id, "transport_speeding",
                    sms_message=f"Speed alert: route '{route.route_name}' recorded {data.speed_kmh:.0f} km/h, above its {route.max_speed_kmh:.0f} km/h limit.",
                    details=f"route_id:{route_id} speed_kmh={data.speed_kmh} limit={route.max_speed_kmh}",
                )
            except Exception:
                logger.exception("Failed to raise transport_speeding incident")

    # Arrival geofence — auto-transitions every EN_ROUTE_BUS student on this
    # route once the bus is within range of the configured destination.
    # Idempotent by construction: _mark_route_students_arrived only ever
    # touches students still EN_ROUTE_BUS, so once they're moved past that
    # state, a later ping inside the same geofence is a no-op.
    if route.destination_lat is not None and route.destination_lng is not None and route.arrival_geofence_meters is not None:
        distance = haversine_distance_meters(data.lat, data.lng, route.destination_lat, route.destination_lng)
        if distance <= route.arrival_geofence_meters:
            try:
                await _mark_route_students_arrived(
                    db, route.school_id, route_id, driver_staff_id,
                    note=f"Route {route_id} auto-arrived (GPS within {route.arrival_geofence_meters:.0f}m of destination)",
                )
            except Exception:
                logger.exception("Failed to auto-mark route arrived from geofence")

    return {"recorded": True, "recorded_at": loc.recorded_at}


@router.get("/transport/{route_id}/location")
async def get_bus_location(
    route_id: str,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Get the latest bus position for a route"""
    route_result = await db.exec(select(TransportRoute).where(TransportRoute.id == route_id))
    route = route_result.first()
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")
    assert_school_access(current_user, route.school_id)

    result = await db.exec(
        select(LiveBusLocation).where(
            LiveBusLocation.route_id == route_id
        ).order_by(LiveBusLocation.recorded_at.desc())
    )
    latest = result.first()
    if not latest:
        raise HTTPException(status_code=404, detail="No location data for this route")
    return latest


@router.get("/transport/{route_id}/stops/eta")
async def get_route_stops_eta(
    route_id: str,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Distance (and, when the latest GPS ping reported a speed, a rough
    ETA) from the bus's current position to each of this route's
    GPS-coordinate stops (routers/transport.py's RouteStop) — previously
    Route.intermediate_stops was just a flat list of names with no
    coordinates, so no per-student ETA was ever computable. Purely a read;
    never mutates arrival state (see post_bus_location's geofence logic for
    the actual auto-arrival trigger)."""
    route_result = await db.exec(select(TransportRoute).where(TransportRoute.id == route_id))
    route = route_result.first()
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")
    assert_school_access(current_user, route.school_id)

    stops_result = await db.exec(select(RouteStop).where(RouteStop.route_id == route_id).order_by(RouteStop.sequence))
    stops = stops_result.all()
    if not stops:
        return {"route_id": route_id, "stops": [], "message": "No GPS-coordinate stops configured for this route"}

    location_result = await db.exec(
        select(LiveBusLocation).where(LiveBusLocation.route_id == route_id).order_by(LiveBusLocation.recorded_at.desc())
    )
    latest = location_result.first()

    entries = []
    for stop in stops:
        distance_meters = None
        eta_minutes = None
        if latest:
            distance_meters = round(haversine_distance_meters(latest.lat, latest.lng, stop.lat, stop.lng), 1)
            if latest.speed_kmh and latest.speed_kmh > 0:
                eta_minutes = round((distance_meters / 1000.0) / latest.speed_kmh * 60.0, 1)
        entries.append({
            "stop_id": stop.id, "sequence": stop.sequence, "name": stop.name,
            "lat": stop.lat, "lng": stop.lng, "eta_offset_minutes": stop.eta_offset_minutes,
            "distance_meters": distance_meters, "eta_minutes": eta_minutes,
        })

    return {
        "route_id": route_id,
        "bus_position": {"lat": latest.lat, "lng": latest.lng, "recorded_at": latest.recorded_at} if latest else None,
        "stops": entries,
    }


# ── Collector Location Sharing ────────────────────────────────────────────────

@router.get("/collector/{session_token}")
async def get_collector_session(
    session_token: str,
    db: AsyncSession = Depends(get_session),
):
    """
    Collector opens the link from the officer's screen.
    No login required — token is the authentication.
    Returns session info so the collector's page can confirm who they are collecting.
    """
    result = await db.exec(
        select(CollectorTrackingSession).where(
            CollectorTrackingSession.session_token == session_token
        )
    )
    session = result.first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.is_active:
        raise HTTPException(status_code=410, detail="This tracking session has ended")

    student_result = await db.exec(
        select(Student).where(Student.id == session.student_id)
    )
    student = student_result.first()

    return {
        "session_id": session.id,
        "session_token": session.session_token,
        "student_name": f"{student.first_name} {student.last_name}" if student else "Student",
        "started_at": session.started_at,
        "is_active": session.is_active,
    }


@router.post("/collector/{session_token}/location")
async def post_collector_location(
    session_token: str,
    data: CollectorLocationCreate,
    db: AsyncSession = Depends(get_session),
):
    """
    Collector's phone posts GPS coordinates.
    No login required — session_token is the auth.
    Called every 10s from the collector-share page.
    """
    result = await db.exec(
        select(CollectorTrackingSession).where(
            CollectorTrackingSession.session_token == session_token
        )
    )
    session = result.first()
    if not session or not session.is_active:
        raise HTTPException(status_code=410, detail="Session not found or already ended")

    loc = CollectorLiveLocation(
        session_id=session.id,
        lat=data.lat,
        lng=data.lng,
    )
    db.add(loc)
    await db.commit()
    return {"recorded": True}


@router.get("/collector/{session_token}/location/live")
async def get_collector_live_location(
    session_token: str,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Parent polls this endpoint to get the collector's latest position"""
    session_result = await db.exec(
        select(CollectorTrackingSession).where(
            CollectorTrackingSession.session_token == session_token
        )
    )
    session = session_result.first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    await assert_parent_of_student(current_user, session.student_id, db)

    loc_result = await db.exec(
        select(CollectorLiveLocation).where(
            CollectorLiveLocation.session_id == session.id
        ).order_by(CollectorLiveLocation.recorded_at.desc())
    )
    latest = loc_result.first()
    if not latest:
        return {"lat": None, "lng": None, "is_active": session.is_active}

    return {
        "lat": latest.lat,
        "lng": latest.lng,
        "recorded_at": latest.recorded_at,
        "is_active": session.is_active,
    }


@router.post("/collector/{session_token}/end")
async def end_collector_session(
    session_token: str,
    db: AsyncSession = Depends(get_session),
):
    """End a collector tracking session.

    No login required — session_token is the auth, matching post_collector_location.
    Called both by the parent confirming safe arrival AND by the collector's own
    public share page ("Child is home" button), which has no way to authenticate.
    """
    result = await db.exec(
        select(CollectorTrackingSession).where(
            CollectorTrackingSession.session_token == session_token
        )
    )
    session = result.first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    session.is_active = False
    session.ended_at = datetime.utcnow()
    db.add(session)
    await db.commit()
    return {"ended": True, "ended_at": session.ended_at}


# ── Admin Overview ────────────────────────────────────────────────────────────

@router.get("/admin/overview")
async def get_admin_overview(
    school_id: str,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*OFFICER_ROLES)),
):
    """
    Single endpoint for the admin tracking page.
    Returns all students with statuses, latest note, and latest bus positions.
    """
    assert_school_access(current_user, school_id)

    students_result = await db.exec(
        select(StudentSecurityProfile, Student).join(
            Student, Student.id == StudentSecurityProfile.student_id
        ).where(StudentSecurityProfile.school_id == school_id)
    )
    rows = students_result.all()

    # Roll back statuses left over from a prior school day
    now = datetime.utcnow()
    stale_reset = False
    for profile, _student in rows:
        if reset_profile_if_stale(profile, now):
            db.add(profile)
            stale_reset = True
    if stale_reset:
        await db.commit()

    # Build class name lookup to avoid N+1 queries
    class_ids = {student.class_id for _, student in rows if student.class_id}
    class_names: dict[str, str] = {}
    if class_ids:
        classes_result = await db.exec(select(Class).where(Class.id.in_(class_ids)))
        for cls in classes_result.all():
            class_names[cls.id] = cls.name

    restricted_ids = await get_restricted_student_ids(db, [student.id for _, student in rows])

    student_data = []
    for profile, student in rows:
        last_note_result = await db.exec(
            select(ParentNote).where(
                ParentNote.student_id == profile.student_id
            ).order_by(ParentNote.created_at.desc())
        )
        last_note = last_note_result.first()

        collector_result = await db.exec(
            select(CollectorTrackingSession).where(
                and_(
                    CollectorTrackingSession.student_id == profile.student_id,
                    CollectorTrackingSession.is_active == True,
                )
            )
        )
        collector = collector_result.first()

        student_data.append({
            "id": student.id,
            "name": f"{student.first_name} {student.last_name}",
            "class": class_names.get(student.class_id, student.class_id),
            "gender": student.gender,
            "pickup_method": profile.pickup_method,
            "transport_route_id": profile.transport_route_id,
            "ghana_post_address": profile.ghana_post_address,
            "neighbourhood": profile.neighbourhood,
            "home_lat": profile.home_lat,
            "home_lng": profile.home_lng,
            "arrival_status": profile.arrival_status,
            "arrival_time": profile.arrival_time,
            "confirmed_at": profile.confirmed_at,
            "release_hold": profile.release_hold,
            "release_hold_reason": profile.release_hold_reason,
            "has_custody_restriction": student.id in restricted_ids,
            "current_location": profile.current_location,
            "collector_session_token": collector.session_token if collector else None,
            "last_note": {
                "text": last_note.text,
                "author_id": last_note.author_id,
                "created_at": last_note.created_at,
                "is_confirmation": last_note.is_confirmation,
            } if last_note else None,
        })

    return {"students": student_data, "as_of": datetime.utcnow()}


@router.get("/admin/scan-logs")
async def list_scan_logs(
    school_id: str,
    days: int = 7,
    result: Optional[ScanResult] = None,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*OFFICER_ROLES)),
):
    """Audit trail of gate scan attempts (authorized and unauthorized) for the
    admin tracking page — who scanned what, when, and what happened."""
    assert_school_access(current_user, school_id)

    cutoff = datetime.utcnow() - timedelta(days=max(1, min(days, 90)))
    query = select(SecurityScanLog).where(
        and_(
            SecurityScanLog.school_id == school_id,
            SecurityScanLog.created_at >= cutoff,
        )
    )
    if result:
        query = query.where(SecurityScanLog.result == result)
    query = query.order_by(SecurityScanLog.created_at.desc()).offset(max(0, offset)).limit(min(max(1, limit), 200))

    logs_result = await db.exec(query)
    logs = logs_result.all()

    student_ids = {log.student_id for log in logs if log.student_id}
    students_by_id = {}
    if student_ids:
        students_result = await db.exec(select(Student).where(Student.id.in_(student_ids)))
        students_by_id = {s.id: s for s in students_result.all()}

    officer_ids = {log.scanned_by_id for log in logs if log.scanned_by_id}
    officers_by_id = {}
    if officer_ids:
        officers_result = await db.exec(select(User).where(User.id.in_(officer_ids)))
        officers_by_id = {u.id: u for u in officers_result.all()}

    entries = []
    for log in logs:
        student = students_by_id.get(log.student_id)
        officer = officers_by_id.get(log.scanned_by_id)
        entries.append({
            "id": log.id,
            "result": log.result,
            "student_id": log.student_id,
            "student_name": f"{student.first_name} {student.last_name}" if student else None,
            "scanned_by_id": log.scanned_by_id,
            "scanned_by_name": f"{officer.first_name} {officer.last_name}" if officer else "Unknown",
            "gate_lat": log.gate_lat,
            "gate_lng": log.gate_lng,
            "created_at": log.created_at,
        })

    return {"logs": entries, "count": len(entries)}


# ── Emergency Pickup Lockdown ────────────────────────────────────────────────
# A school-wide "no student can be released right now" switch for a crisis —
# previously every action in this module was per-student; there was no way
# to suspend all releases at once. Enforced via assert_release_allowed
# (QR scan, staff status change, parent on-my-way/confirm) and
# assert_no_pickup_lockdown (transport dispatch).

@router.get("/admin/lockdown")
async def get_pickup_lockdown_status(
    school_id: str,
    current_user: User = Depends(require_roles(*OFFICER_ROLES)),
    db: AsyncSession = Depends(get_session),
):
    assert_school_access(current_user, school_id)
    school = await db.get(School, school_id)
    if not school:
        raise HTTPException(status_code=404, detail="School not found")
    return {
        "pickup_lockdown_active": school.pickup_lockdown_active,
        "reason": school.pickup_lockdown_reason,
        "set_by": school.pickup_lockdown_set_by,
        "set_at": school.pickup_lockdown_set_at,
    }


@router.post("/admin/lockdown")
async def set_pickup_lockdown(
    school_id: str,
    body: SetLockdownRequest,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    _plan_check: User = Depends(require_plan_feature("student_safety")),
    db: AsyncSession = Depends(get_session),
):
    """Admin-only (not security officers) — this suspends release for
    every student in the school at once, a much bigger action than an
    individual release-hold, so it's scoped like the court-order custody
    restriction above rather than the officer-settable release-hold."""
    assert_school_access(current_user, school_id)
    if body.active and not (body.reason or "").strip():
        raise HTTPException(status_code=400, detail="A reason is required to activate a pickup lockdown")

    school = await db.get(School, school_id)
    if not school:
        raise HTTPException(status_code=404, detail="School not found")

    now = datetime.utcnow()
    school.pickup_lockdown_active = body.active
    school.pickup_lockdown_reason = body.reason.strip() if body.active and body.reason else None
    school.pickup_lockdown_set_by = current_user.id if body.active else None
    school.pickup_lockdown_set_at = now if body.active else None
    db.add(school)
    await db.commit()

    incident_type = "lockdown_activated" if body.active else "lockdown_deactivated"
    sms_message = (
        f"EMERGENCY: a pickup lockdown has been ACTIVATED for {school.name}. {school.pickup_lockdown_reason or ''}".strip()
        if body.active else
        f"The pickup lockdown for {school.name} has been lifted."
    )
    try:
        await security_alert_service.raise_incident(
            db, school_id, incident_type, sms_message=sms_message,
            related_user_id=current_user.id, details=school.pickup_lockdown_reason,
        )
    except Exception:
        logger.exception(f"Failed to raise {incident_type} incident")
    try:
        await broadcaster.publish({
            "type": incident_type, "school_id": school_id, "reason": school.pickup_lockdown_reason,
        })
    except Exception:
        logger.exception(f"Failed to publish {incident_type} event")

    return {"pickup_lockdown_active": school.pickup_lockdown_active, "reason": school.pickup_lockdown_reason}


# ── Security Incidents ───────────────────────────────────────────────────────
# A persisted, must-be-acknowledged record for safeguarding-relevant events
# (custody-restriction attempts, unauthorized scans, unresolved late
# pickups, lockdowns) — see models.security.SecurityIncident and
# services.security_alert_service. Previously these events only ever fired
# a transient SSE broadcast (+ SMS for some), with no persistent pattern
# tracking and no human-must-close-out workflow, unlike Discipline's
# maker-checker incident process.

@router.get("/admin/incidents")
async def list_security_incidents(
    school_id: str,
    incident_type: Optional[str] = None,
    acknowledged: Optional[bool] = None,
    days: int = 30,
    limit: int = 100,
    current_user: User = Depends(require_roles(*OFFICER_ROLES)),
    db: AsyncSession = Depends(get_session),
):
    assert_school_access(current_user, school_id)
    cutoff = datetime.utcnow() - timedelta(days=max(1, min(days, 365)))
    query = select(SecurityIncident).where(
        SecurityIncident.school_id == school_id,
        SecurityIncident.created_at >= cutoff,
    )
    if incident_type:
        query = query.where(SecurityIncident.incident_type == incident_type)
    if acknowledged is not None:
        query = query.where(SecurityIncident.acknowledged == acknowledged)
    query = query.order_by(SecurityIncident.created_at.desc()).limit(min(max(1, limit), 200))
    result = await db.exec(query)
    return result.all()


@router.post("/admin/incidents/{incident_id}/acknowledge", response_model=SecurityIncident)
async def acknowledge_security_incident(
    incident_id: str,
    body: AcknowledgeIncidentRequest,
    current_user: User = Depends(require_roles(*OFFICER_ROLES)),
    db: AsyncSession = Depends(get_session),
):
    incident = await db.get(SecurityIncident, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    assert_school_access(current_user, incident.school_id)

    incident.acknowledged = True
    incident.acknowledged_by = current_user.id
    incident.acknowledged_at = datetime.utcnow()
    incident.resolution_notes = body.resolution_notes
    db.add(incident)
    await db.commit()
    await db.refresh(incident)
    return incident


# ── Daily Reset ───────────────────────────────────────────────────────────────

@router.post("/admin/reset-day")
async def reset_daily_status(
    body: SchoolScopedRequest,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
):
    """
    Reset all student arrival statuses to pending for a new school day.
    Call this each morning before QR generation.

    Note: nothing currently schedules this automatically — `get_all_students_status`
    and `get_admin_overview` self-heal stale (prior-day) statuses on every read via
    `reset_profile_if_stale`, so this endpoint is for an explicit manual reset only.
    """
    school_id = body.school_id
    assert_school_access(current_user, school_id)
    result = await db.exec(
        select(StudentSecurityProfile).where(
            StudentSecurityProfile.school_id == school_id
        )
    )
    profiles = result.all()

    for profile in profiles:
        profile.arrival_status = ArrivalStatus.PENDING
        profile.arrival_time = None
        profile.confirmed_at = None
        profile.updated_at = datetime.utcnow()
        db.add(profile)

    await db.commit()
    return {"reset": len(profiles), "date": today_str()}
