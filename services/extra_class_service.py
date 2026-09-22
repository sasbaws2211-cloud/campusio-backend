from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from models.extra_class import (
    BillingInterval,
    ExtraClass,
    ExtraClassBillingCycle,
    ExtraClassEnrollment,
    ExtraClassPayment,
    EnrollmentStatus,
)
from models.payment import OnlineTransaction

ACTIVE_ENROLLMENT_STATUSES = (EnrollmentStatus.APPROVED, EnrollmentStatus.ACTIVE)


def _next_due_date(current_due: datetime, interval: BillingInterval) -> datetime:
    days = 7 if interval == BillingInterval.WEEKLY else 30
    return current_due + timedelta(days=days)


async def check_capacity(session: AsyncSession, extra_class: ExtraClass) -> Tuple[int, int]:
    """Returns (currently_approved_count, max_allowed)."""
    result = await session.execute(
        select(ExtraClassEnrollment).where(
            ExtraClassEnrollment.extra_class_id == extra_class.id,
            ExtraClassEnrollment.status.in_(ACTIVE_ENROLLMENT_STATUSES),
        )
    )
    current = len(result.scalars().all())
    return current, extra_class.max_students_per_session


async def ensure_current_billing_cycle(
    session: AsyncSession, enrollment: ExtraClassEnrollment, extra_class: ExtraClass
) -> Optional[ExtraClassBillingCycle]:
    """Lazily advances an enrollment's billing to the current period.

    Nothing runs this on a schedule — there is no task scheduler in this
    codebase (see subscription_suspension_service.py). It self-heals
    whenever an enrollment's billing is read (parent/student "me" list) or
    immediately after a payment is confirmed, same pattern as
    reset_profile_if_stale() in the security module.

    A ONE_TIME class never recurs. A paid cycle whose due date has passed
    rolls forward into a fresh pending cycle. A cycle that's overdue and
    still unpaid is just flagged 'overdue' — no new debt is invented on top
    of unpaid debt.
    """
    if enrollment.status not in ACTIVE_ENROLLMENT_STATUSES:
        return None
    if extra_class.billing_interval == BillingInterval.ONE_TIME:
        return None

    result = await session.execute(
        select(ExtraClassBillingCycle)
        .where(ExtraClassBillingCycle.enrollment_id == enrollment.id)
        .order_by(ExtraClassBillingCycle.created_at.desc())
    )
    latest = result.scalars().first()
    if not latest:
        return None

    now = datetime.utcnow()

    if latest.status == "pending" and latest.next_due_date and latest.next_due_date < now:
        latest.status = "overdue"
        latest.updated_at = now
        session.add(latest)
        await session.flush()
        return latest

    if latest.status == "paid" and latest.next_due_date and latest.next_due_date <= now:
        new_due = _next_due_date(latest.next_due_date, extra_class.billing_interval)
        new_cycle = ExtraClassBillingCycle(
            school_id=extra_class.school_id,
            extra_class_id=extra_class.id,
            teacher_id=extra_class.teacher_id,
            enrollment_id=enrollment.id,
            parent_id=enrollment.parent_id,
            student_id=enrollment.student_id,
            amount=extra_class.price,
            interval=extra_class.billing_interval,
            next_due_date=new_due,
            status="pending",
        )
        session.add(new_cycle)
        await session.flush()

        session.add(
            ExtraClassPayment(
                school_id=extra_class.school_id,
                billing_cycle_id=new_cycle.id,
                amount=extra_class.price,
                payment_method="pending",
                status="pending",
            )
        )
        await session.flush()
        return new_cycle

    return latest


async def apply_billing_payment(session: AsyncSession, transaction: OnlineTransaction) -> Dict[str, Any]:
    """Called from the Paystack webhook on a verified successful payment."""
    if not transaction.billing_cycle_id:
        return {"success": False, "error": "Transaction has no billing_cycle_id"}

    cycle_result = await session.execute(
        select(ExtraClassBillingCycle).where(ExtraClassBillingCycle.id == transaction.billing_cycle_id)
    )
    billing_cycle = cycle_result.scalar_one_or_none()
    if not billing_cycle:
        return {"success": False, "error": "Billing cycle not found"}

    now = datetime.utcnow()
    billing_cycle.status = "paid"
    billing_cycle.paid_at = now
    billing_cycle.updated_at = now
    session.add(billing_cycle)

    payment_result = await session.execute(
        select(ExtraClassPayment).where(ExtraClassPayment.billing_cycle_id == billing_cycle.id)
    )
    payment = payment_result.scalar_one_or_none()
    if payment:
        payment.amount = transaction.amount_paid
        payment.payment_method = "paystack"
        payment.reference = transaction.reference
        payment.status = "paid"
        payment.paid_at = now
        payment.updated_at = now
        session.add(payment)

    await session.flush()

    enrollment_result = await session.execute(
        select(ExtraClassEnrollment).where(ExtraClassEnrollment.id == billing_cycle.enrollment_id)
    )
    enrollment = enrollment_result.scalar_one_or_none()
    extra_class_result = await session.execute(select(ExtraClass).where(ExtraClass.id == billing_cycle.extra_class_id))
    extra_class = extra_class_result.scalar_one_or_none()
    if enrollment and extra_class:
        await ensure_current_billing_cycle(session, enrollment, extra_class)

    return {"success": True, "billing_cycle_id": billing_cycle.id}
