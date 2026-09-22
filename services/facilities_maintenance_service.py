"""Turns a due MaintenanceSchedule into a real FacilityWorkOrder.

Run nightly by services/scheduler.py across every school, and also
exposed as a manual POST /facilities/schedules/run-due-check for a school
admin who doesn't want to wait for the cron — same dual-trigger pattern as
the billing jobs in scheduler.py.
"""
import logging
from datetime import date, datetime, timedelta

from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.facilities import FacilityWorkOrder, MaintenanceSchedule

logger = logging.getLogger(__name__)

# Schedule.frequency is free text (schools type whatever they like), so
# this only recognizes the common words and falls back to a flat 30 days
# for anything else — better than crashing or never rescheduling.
_FREQUENCY_DAYS = {
    "daily": 1,
    "weekly": 7,
    "fortnightly": 14,
    "biweekly": 14,
    "monthly": 30,
    "quarterly": 91,
    "biannual": 182,
    "semiannual": 182,
    "annually": 365,
    "annual": 365,
    "yearly": 365,
}


def _advance_due_date(current_due_date: str, frequency: str) -> str:
    days = _FREQUENCY_DAYS.get((frequency or "").strip().lower(), 30)
    try:
        current = date.fromisoformat(current_due_date)
    except ValueError:
        current = date.today()
    return (current + timedelta(days=days)).isoformat()


async def run_due_maintenance_schedules(session: AsyncSession, school_id: str | None = None) -> dict:
    """Finds every active MaintenanceSchedule whose next_due_date has
    arrived, opens a FacilityWorkOrder for it, and advances the schedule
    to its next cycle. `school_id=None` runs across every school (the
    nightly cron); a specific id scopes it to one school (the manual
    admin trigger)."""
    today = date.today().isoformat()
    query = select(MaintenanceSchedule).where(
        MaintenanceSchedule.active == True,  # noqa: E712 — SQLAlchemy comparison, not a Python bool check
        MaintenanceSchedule.next_due_date <= today,
    )
    if school_id:
        query = query.where(MaintenanceSchedule.school_id == school_id)
    schedules = (await session.execute(query)).scalars().all()

    created = []
    skipped = []
    for schedule in schedules:
        # Dedupe: don't open a second work order for the same due cycle if
        # one's already open/in-progress against this schedule (e.g. the
        # manual trigger gets hit twice, or a misfire re-runs the cron).
        existing = (await session.execute(
            select(FacilityWorkOrder).where(
                FacilityWorkOrder.schedule_id == schedule.id,
                FacilityWorkOrder.status.in_(["open", "scheduled", "in_progress"]),
            )
        )).first()
        if existing:
            skipped.append(schedule.id)
            continue

        work_order = FacilityWorkOrder(
            school_id=schedule.school_id,
            asset_id=schedule.asset_id,
            room_id=schedule.room_id,
            contractor_id=schedule.contractor_id,
            schedule_id=schedule.id,
            title=f"Preventive maintenance: {schedule.title}",
            description=f"Auto-generated from the '{schedule.frequency}' preventive maintenance schedule (due {schedule.next_due_date}).",
            priority="normal",
            requested_by="SYSTEM",
            assigned_to=schedule.assigned_to,
            requested_date=today,
        )
        session.add(work_order)
        schedule.next_due_date = _advance_due_date(schedule.next_due_date, schedule.frequency)
        session.add(schedule)
        created.append(schedule.id)

    if created or skipped:
        await session.commit()

    logger.info(f"Preventive maintenance sweep: {len(created)} work order(s) created, {len(skipped)} schedule(s) already had one open")
    return {"schedules_checked": len(schedules), "work_orders_created": len(created), "skipped_already_open": len(skipped)}
