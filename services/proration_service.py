"""Proration service for mid-term student-count growth (Phase 2)

Subscriptions are write-once billing snapshots (see platform_billing_service.py) —
student_count is fixed at generation time and never recalculated. That's
correct for the common case, but a school that enrolls new students partway
through a term is using the platform for those extra seats without ever
being billed for them. This service closes that gap: it periodically compares
each subscription's already-billed student count against the school's current
active count and charges a prorated amount for any net growth, scaled by the
fraction of the billing period remaining.

Design choices, deliberately:
- Only growth is charged. A student count *decrease* issues no mid-term
  credit/refund — it's cash-negative and this codebase's refund story is
  manual reconciliation only (see mark_transaction_refunded). Shrinkage
  simply isn't billed for going forward; the next term's subscription
  generation already uses the live count.
- reconciled_student_count tracks the *peak* count ever billed/prorated for
  a subscription, and only ever moves up. This makes the whole thing
  idempotent and safe against dip-then-recover noise: if a school's count
  drops from 100 to 90 and back to 100, that nets to zero net growth beyond
  the original 100 already paid for — it must not be charged again.
- The collectible total (subscription_outstanding()) already includes
  proration_adjustment, so payment, suspension, reactivation, reminders, and
  reporting all pick this up automatically without any changes there.
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from models.billing import PlatformSubscription, ProrationCharge, SubscriptionStatus
from models.school import AcademicTerm
from models.student import Student, StudentStatus

logger = logging.getLogger(__name__)


def calculate_prorated_amount(
    student_delta: int,
    unit_price: float,
    period_start: datetime,
    period_end: datetime,
    as_of: datetime,
) -> Dict:
    """Pure proration math: the charge for `student_delta` additional
    students, scaled by the fraction of [period_start, period_end] still
    remaining as of `as_of`. Never raises — a period that hasn't started or
    has already ended clamps to a 0.0 or full-price result rather than
    erroring, so callers can log the result without branching first.
    """
    period_total_days = max((period_end - period_start).days, 1)
    remaining_days = (period_end - as_of).days
    remaining_days = max(0, min(remaining_days, period_total_days))
    fraction = remaining_days / period_total_days
    prorated_amount = round(student_delta * unit_price * fraction, 2)
    return {
        "period_total_days": period_total_days,
        "remaining_days": remaining_days,
        "fraction": fraction,
        "prorated_amount": prorated_amount,
    }


class ProrationService:
    """Detects and charges for mid-term student-count growth."""

    async def _get_period_bounds(
        self, session: AsyncSession, subscription: PlatformSubscription
    ) -> Optional[Tuple[datetime, datetime]]:
        """The coverage period this subscription pays for: the academic
        term's dates for the termly plan, the calendar month for the
        monthly plan (billing_month is "YYYY-MM")."""
        if subscription.billing_month:
            try:
                year, month = (int(part) for part in subscription.billing_month.split("-"))
            except (ValueError, AttributeError):
                return None
            start = datetime(year, month, 1)
            end = (datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)) - timedelta(days=1)
            return start, end

        if not subscription.academic_term_id:
            return None

        result = await session.execute(
            select(AcademicTerm).where(AcademicTerm.id == subscription.academic_term_id)
        )
        term = result.scalar_one_or_none()
        if not term:
            return None

        try:
            start = datetime.strptime(term.start_date, "%Y-%m-%d")
            end = datetime.strptime(term.end_date, "%Y-%m-%d")
        except (ValueError, TypeError):
            return None
        return start, end

    async def check_and_apply_proration(
        self,
        session: AsyncSession,
        school_id: Optional[str] = None,
    ) -> Dict:
        """Check subscriptions for net student growth beyond what's already
        been billed and charge a prorated amount for the delta.

        school_id=None checks every school — the same pattern used by
        late fees and suspension checks.
        """
        try:
            query = select(PlatformSubscription).where(
                PlatformSubscription.status.in_([
                    SubscriptionStatus.PENDING,
                    SubscriptionStatus.ACTIVE,
                    SubscriptionStatus.SUSPENDED,
                ])
            )
            if school_id:
                query = query.where(PlatformSubscription.school_id == school_id)
            result = await session.execute(query)
            subscriptions = result.scalars().all()

            now = datetime.utcnow()
            count = 0
            total_charged = 0.0

            for sub in subscriptions:
                bounds = await self._get_period_bounds(session, sub)
                if not bounds:
                    continue
                period_start, period_end = bounds
                if now > period_end:
                    continue  # period already over — next term's snapshot handles the new count

                baseline = sub.reconciled_student_count if sub.reconciled_student_count is not None else sub.student_count

                students_result = await session.execute(
                    select(Student).where(
                        Student.school_id == sub.school_id,
                        Student.status == StudentStatus.ACTIVE,
                    )
                )
                current_count = len(students_result.scalars().all())

                if current_count <= baseline:
                    continue  # shrinkage or unchanged — no charge, no credit (see module docstring)

                delta = current_count - baseline
                calc = calculate_prorated_amount(delta, sub.unit_price, period_start, period_end, now)
                if calc["prorated_amount"] <= 0:
                    continue

                charge = ProrationCharge(
                    subscription_id=sub.id,
                    school_id=sub.school_id,
                    previous_student_count=baseline,
                    new_student_count=current_count,
                    student_delta=delta,
                    unit_price=sub.unit_price,
                    period_start=period_start,
                    period_end=period_end,
                    period_total_days=calc["period_total_days"],
                    remaining_days=calc["remaining_days"],
                    prorated_amount=calc["prorated_amount"],
                )
                session.add(charge)

                # Same defensive derivation late_fee_service.py uses —
                # after_discount is 0.0 on rows created before it was
                # initialized, so recompute from total_amount_due directly
                # rather than trusting the stored value.
                base_after_discount = round(sub.total_amount_due - sub.discount_amount, 2)
                sub.proration_adjustment = round(sub.proration_adjustment + calc["prorated_amount"], 2)
                sub.reconciled_student_count = current_count
                sub.after_discount = base_after_discount
                sub.final_amount_due = round(
                    base_after_discount + sub.late_fee_amount + sub.proration_adjustment, 2
                )
                sub.updated_at = now

                count += 1
                total_charged += calc["prorated_amount"]

                logger.info(
                    f"Prorated subscription {sub.id}: +{delta} students "
                    f"({baseline} -> {current_count}), charged GHS {calc['prorated_amount']}"
                )

            if count:
                await session.commit()

            return {
                "success": True,
                "subscriptions_prorated": count,
                "total_charged": round(total_charged, 2),
                "message": f"Applied proration charges to {count} subscriptions",
            }

        except Exception as e:
            logger.error(f"Error applying proration: {str(e)}")
            await session.rollback()
            return {"success": False, "error": str(e)}

    async def list_subscriptions_overview(
        self,
        session: AsyncSession,
        school_id: Optional[str] = None,
    ) -> List[Dict]:
        """List subscriptions with an active billing period, showing the
        billed baseline vs the school's live student count. Includes a
        *preview* of what "Apply Proration" would charge right now for any
        pending (not-yet-billed) growth — computed the same way
        check_and_apply_proration does, but without writing anything.

        Only subscriptions with a resolvable, still-current period are
        included — the same set check_and_apply_proration would consider.
        """
        query = select(PlatformSubscription).where(
            PlatformSubscription.status.in_([
                SubscriptionStatus.PENDING,
                SubscriptionStatus.ACTIVE,
                SubscriptionStatus.SUSPENDED,
            ])
        )
        if school_id:
            query = query.where(PlatformSubscription.school_id == school_id)
        result = await session.execute(query.order_by(PlatformSubscription.created_at.desc()))
        subscriptions = result.scalars().all()

        now = datetime.utcnow()
        overview = []

        for sub in subscriptions:
            bounds = await self._get_period_bounds(session, sub)
            if not bounds:
                continue
            period_start, period_end = bounds
            if now > period_end:
                continue

            baseline = sub.reconciled_student_count if sub.reconciled_student_count is not None else sub.student_count

            students_result = await session.execute(
                select(Student).where(
                    Student.school_id == sub.school_id,
                    Student.status == StudentStatus.ACTIVE,
                )
            )
            current_count = len(students_result.scalars().all())
            pending_delta = max(0, current_count - baseline)

            # Computed even when pending_delta is 0 (prorated_amount comes out
            # 0.0 either way) so remaining_days always reflects the period's
            # actual state, not just "no pending charge".
            preview = calculate_prorated_amount(pending_delta, sub.unit_price, period_start, period_end, now)

            overview.append({
                "subscription_id": sub.id,
                "school_id": sub.school_id,
                "status": sub.status.value if hasattr(sub.status, "value") else sub.status,
                "billed_student_count": sub.student_count,
                "reconciled_student_count": baseline,
                "current_student_count": current_count,
                "pending_delta": pending_delta,
                "pending_charge_preview": preview["prorated_amount"],
                "already_charged": sub.proration_adjustment,
                "period_end": period_end.isoformat(),
                "remaining_days": preview["remaining_days"],
            })

        return overview

    async def get_proration_history(
        self,
        session: AsyncSession,
        subscription_id: str,
    ) -> List[Dict]:
        """Proration charge history for a subscription, most recent first."""
        result = await session.execute(
            select(ProrationCharge)
            .where(ProrationCharge.subscription_id == subscription_id)
            .order_by(ProrationCharge.applied_date.desc())
        )
        charges = result.scalars().all()
        return [
            {
                "id": c.id,
                "previous_student_count": c.previous_student_count,
                "new_student_count": c.new_student_count,
                "student_delta": c.student_delta,
                "unit_price": c.unit_price,
                "period_start": c.period_start.isoformat(),
                "period_end": c.period_end.isoformat(),
                "remaining_days": c.remaining_days,
                "period_total_days": c.period_total_days,
                "prorated_amount": c.prorated_amount,
                "applied_date": c.applied_date.isoformat(),
            }
            for c in charges
        ]
