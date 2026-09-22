"""Bridges a transport dropoff attendance record (routers/transport.py's
TransportAttendance, trip_type="dropoff") into the same ArrivalStatus safety
state machine that parent-pickup students go through (routers/security.py) —
previously the two were entirely disconnected: a transport-pickup student
stayed EN_ROUTE_BUS forever as far as the security/pickup screens were
concerned, even after a teacher had recorded them getting off the bus at
their stop. Deliberately in its own module (not imported from
routers/security.py or vice versa) since routers/security.py already imports
from routers/transport.py (get_driver_route_ids) — importing the other way
back would be circular.
"""
import logging
from typing import Optional

from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.security import ArrivalEvent, ArrivalEventType, ArrivalStatus, StudentSecurityProfile

logger = logging.getLogger(__name__)


async def sync_dropoff_to_arrival_status(
    session: AsyncSession, school_id: str, student_id: str, triggered_by_id: Optional[str],
) -> None:
    """Called after a trip_type="dropoff", status=PRESENT TransportAttendance
    row is recorded. Moves the student's StudentSecurityProfile to
    ARRIVED_UNCONFIRMED (not straight to SAFE_CONFIRMED) — a teacher marking
    "got off the bus at the stop" confirms the drop-off happened, not that a
    parent/guardian was actually there to receive the child, so the final
    safe-confirmed step is left to the existing parent-confirmation or
    staff-status-change paths, same as every other arrival route. Never
    raises: this is a best-effort bridge, matching this codebase's
    established "log and swallow" contract for cross-system sync (see
    services/assignment_grade_bridge.py, services/exam_result_aggregation_service.py).
    """
    try:
        profile_result = await session.execute(
            select(StudentSecurityProfile).where(StudentSecurityProfile.student_id == student_id)
        )
        profile = profile_result.scalar_one_or_none()
        if not profile:
            return
        if profile.arrival_status == ArrivalStatus.SAFE_CONFIRMED:
            return  # already closed out via some other path — don't regress it

        from datetime import datetime
        now = datetime.utcnow()
        profile.arrival_status = ArrivalStatus.ARRIVED_UNCONFIRMED
        profile.arrival_time = now
        profile.updated_at = now
        session.add(profile)

        session.add(ArrivalEvent(
            school_id=school_id,
            student_id=student_id,
            event_type=ArrivalEventType.ARRIVED,
            triggered_by_id=triggered_by_id or "system:transport_attendance",
            notes="Transport dropoff recorded",
        ))
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception(f"Failed to sync transport dropoff to arrival status for student {student_id}")
