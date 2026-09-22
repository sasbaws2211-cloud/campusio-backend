"""Small helpers shared between leave requests and auto-absent marking — both
need the same "is this a working day" definition so they don't drift apart.
Weekends always count as non-working; callers may additionally pass the
school's holiday dates (from the `calendar_events` table — see
fetch_holiday_dates below) to exclude those too. Kept pure/sync — the caller
does the one DB fetch and passes the resulting set in, rather than every date
check becoming an async DB round-trip.
"""
from datetime import datetime, timedelta
from typing import List, Optional, Set

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.school import CalendarEvent

WEEKEND_WEEKDAYS = (5, 6)  # Saturday, Sunday


def is_working_day(date_str: str, holiday_dates: Optional[Set[str]] = None) -> bool:
    if holiday_dates and date_str in holiday_dates:
        return False
    return datetime.strptime(date_str, "%Y-%m-%d").weekday() not in WEEKEND_WEEKDAYS


def working_days_in_range(start_date: str, end_date: str, holiday_dates: Optional[Set[str]] = None) -> List[str]:
    """Inclusive list of working-day date strings between start_date and end_date."""
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    days = []
    current = start
    while current <= end:
        date_str = current.strftime("%Y-%m-%d")
        if is_working_day(date_str, holiday_dates):
            days.append(date_str)
        current += timedelta(days=1)
    return days


async def fetch_holiday_dates(session: AsyncSession, school_id: str) -> Set[str]:
    """Every date covered by a non-instructional CalendarEvent for this school,
    expanded from each event's [start_date, end_date] range into individual
    YYYY-MM-DD strings so is_working_day/working_days_in_range can do a plain
    set lookup."""
    result = await session.execute(
        select(CalendarEvent).where(CalendarEvent.school_id == school_id, CalendarEvent.is_instructional == False)
    )
    dates: Set[str] = set()
    for event in result.scalars().all():
        try:
            start = datetime.strptime(event.start_date, "%Y-%m-%d")
            end = datetime.strptime(event.end_date, "%Y-%m-%d")
        except ValueError:
            continue
        current = start
        while current <= end:
            dates.add(current.strftime("%Y-%m-%d"))
            current += timedelta(days=1)
    return dates
