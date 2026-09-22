"""Proactive escalation for student-safety events that previously only fired
a transient SSE broadcast (nobody notified unless a screen happened to be
open) or, at most, an SMS to the parent. Two things this adds:

1. A persisted, must-be-acknowledged SecurityIncident row — so a pattern
   (e.g. the same restricted parent trying repeatedly, or a device going
   quiet) is visible after the fact, not just live-broadcast-or-nothing.
2. A best-effort SMS to the school's own admin/security staff, mirroring
   services/attendance_risk_service.py's "SMS the people who need to act on
   this" shape rather than an in-app Message (which needs a sender_id —
   there's no natural "actor" for a system-triggered alert).
"""
import logging
from datetime import datetime
from typing import Optional

from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.security import SecurityIncident
from models.user import User, UserRole
from services.sms_service import sms_service

logger = logging.getLogger(__name__)

ALERT_RECIPIENT_ROLES = (UserRole.SCHOOL_ADMIN, UserRole.SECURITY_OFFICER)


async def log_incident(
    session: AsyncSession,
    school_id: str,
    incident_type: str,
    student_id: Optional[str] = None,
    details: Optional[str] = None,
    related_user_id: Optional[str] = None,
) -> SecurityIncident:
    """Persist the incident. Commits immediately (small, standalone row) so
    it survives even if the caller's own transaction later rolls back."""
    incident = SecurityIncident(
        school_id=school_id,
        student_id=student_id,
        incident_type=incident_type,
        details=details,
        related_user_id=related_user_id,
    )
    session.add(incident)
    await session.commit()
    await session.refresh(incident)
    return incident


async def notify_school_security_staff(session: AsyncSession, school_id: str, message: str) -> int:
    """Best-effort SMS to every SCHOOL_ADMIN/SECURITY_OFFICER in this school
    with a phone number on file. Never raises — an alerting failure must
    never block the action that triggered it. Returns the number of
    successful sends."""
    try:
        staff_result = await session.execute(
            select(User).where(User.school_id == school_id, User.role.in_(ALERT_RECIPIENT_ROLES), User.is_active == True)  # noqa: E712
        )
        staff = staff_result.scalars().all()
    except Exception:
        logger.exception("Failed to look up school security staff for alerting")
        return 0

    sent = 0
    for user in staff:
        if not user.phone or not sms_service.validate_phone_number(user.phone):
            continue
        try:
            result = await sms_service.send_sms([sms_service.format_phone_number(user.phone)], message)
            if result.get("success"):
                sent += 1
        except Exception:
            logger.exception(f"Failed to send security alert SMS to staff user {user.id}")
    return sent


async def raise_incident(
    session: AsyncSession,
    school_id: str,
    incident_type: str,
    sms_message: str,
    student_id: Optional[str] = None,
    details: Optional[str] = None,
    related_user_id: Optional[str] = None,
) -> SecurityIncident:
    """Combines log_incident + notify_school_security_staff — the usual
    call shape for a safeguarding event that needs both a durable record and
    an immediate human alert."""
    incident = await log_incident(session, school_id, incident_type, student_id, details, related_user_id)
    try:
        await notify_school_security_staff(session, school_id, sms_message)
    except Exception:
        logger.exception("Failed to notify school security staff of incident")
    return incident
