"""Shared HR-development helpers — currently just the standard onboarding
checklist, called both automatically on hire (routers/hr_recruitment.py)
and manually for existing staff who never went through that pipeline
(routers/hr_development.py's own seed endpoint)."""
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from models.hr_development import StaffOnboardingTask

DEFAULT_ONBOARDING_TASKS = [
    ("Submit signed employment contract", 3),
    ("Submit valid ID / passport copy", 3),
    ("Submit SSNIT number", 7),
    ("Submit bank/mobile money details for payroll", 7),
    ("Complete orientation session", 14),
    ("Issue staff ID card", 14),
    ("Set up system/portal login", 3),
]


async def seed_default_onboarding_checklist(session: AsyncSession, school_id: str, staff_id: str, created_by: str) -> int:
    """Creates the standard onboarding checklist for one staff member,
    skipping any title that already exists for them (safe to call more
    than once — e.g. a school re-running it after adding a task
    manually). due_date is relative to today, in days. Returns how many
    tasks were actually created."""
    existing_result = await session.execute(
        select(StaffOnboardingTask.title).where(StaffOnboardingTask.staff_id == staff_id)
    )
    existing_titles = {row[0] for row in existing_result.all()}

    created = 0
    today = datetime.utcnow()
    for title, days_offset in DEFAULT_ONBOARDING_TASKS:
        if title in existing_titles:
            continue
        session.add(StaffOnboardingTask(
            school_id=school_id, staff_id=staff_id, title=title,
            due_date=(today + timedelta(days=days_offset)).strftime("%Y-%m-%d"),
            created_by=created_by,
        ))
        created += 1
    return created
