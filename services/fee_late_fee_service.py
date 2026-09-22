"""Automated late-fee penalty application for overdue student fees —
distinct from services/late_fee_service.py, which only handles Campusio's
own platform-subscription billing to SCHOOLS, not student fees to parents.

Mirrors services/fee_reminder_service.py's shape (per-school settings,
idempotent via a marker column) since it shares the same due-date logic."""
import logging
from datetime import date, datetime
from typing import Dict, Optional

from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import async_session
from models.fee import Fee, PaymentStatus
from models.fee_reminders import FeeReminderSettings
from services.fee_reminder_service import get_or_create_settings, _effective_due_date

logger = logging.getLogger(__name__)


async def apply_late_fees(session: AsyncSession, school_id: Optional[str] = None) -> Dict:
    """Charge a one-time late-fee penalty on every fee that's past its
    grace period and hasn't already been charged (Fee.late_fee_amount == 0
    is the idempotency guard — safe to call this repeatedly, e.g. from both
    the nightly sweep and an admin's manual run-now)."""
    today = date.today()
    settings_query = select(FeeReminderSettings)
    if school_id:
        settings_query = settings_query.where(FeeReminderSettings.school_id == school_id)
    settings_rows = (await session.execute(settings_query)).scalars().all()

    school_ids = {school_id} if school_id else set()
    if not school_id:
        fee_school_ids = (await session.execute(select(Fee.school_id).distinct())).scalars().all()
        school_ids.update(fee_school_ids)

    settings_by_school = {s.school_id: s for s in settings_rows}
    fees_charged = 0
    total_charged = 0.0

    for sid in school_ids:
        settings = settings_by_school.get(sid) or await get_or_create_settings(session, sid)
        if not settings.enabled or not settings.enable_late_fees or settings.late_fee_percentage <= 0:
            continue

        fees = (
            await session.execute(
                select(Fee).where(
                    Fee.school_id == sid,
                    Fee.status.in_([PaymentStatus.PENDING.value, PaymentStatus.PARTIAL.value, PaymentStatus.OVERDUE.value]),
                    Fee.late_fee_amount == 0,
                )
            )
        ).scalars().all()

        for fee in fees:
            outstanding = fee.amount_due - fee.amount_paid - fee.discount
            if outstanding <= 0:
                continue
            due_date_str = await _effective_due_date(session, fee)
            if not due_date_str:
                continue
            try:
                due_date = date.fromisoformat(due_date_str)
            except ValueError:
                continue
            days_overdue = (today - due_date).days
            if days_overdue < settings.late_fee_grace_days:
                continue

            penalty = round(outstanding * settings.late_fee_percentage / 100, 2)
            if penalty <= 0:
                continue

            fee.amount_due += penalty
            fee.late_fee_amount = penalty
            fee.status = PaymentStatus.OVERDUE
            fee.updated_at = datetime.utcnow()
            session.add(fee)
            fees_charged += 1
            total_charged += penalty

    await session.commit()
    return {"fees_charged": fees_charged, "total_late_fees_applied": round(total_charged, 2)}


async def run_late_fee_sweep() -> None:
    """Called by services/scheduler.py — every school, every day."""
    async with async_session() as session:
        result = await apply_late_fees(session, school_id=None)
        logger.info(
            f"Late fee sweep: {result['fees_charged']} fee(s) charged, "
            f"total GHS {result['total_late_fees_applied']:.2f}"
        )
