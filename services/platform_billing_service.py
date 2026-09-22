"""Platform billing service - manages school subscription to platform

Ledger rules this module enforces:
- Subscriptions and invoices are write-once snapshots: student_count and
  unit_price are captured at generation time and never recalculated.
- One subscription per (school, term) on the termly plan, one per
  (school, month) on the monthly plan — backed by partial unique indexes
  on platform_subscriptions, so a race can't create a double bill.
- Payment application is idempotent: a Paystack webhook replay (they retry
  until acknowledged) or a concurrent manual verify never applies the same
  money twice. The OnlineTransaction row is the idempotency key, locked
  FOR UPDATE while applying.
- No writes to the school's GL: platform fees are Campusio revenue, not
  school revenue — the subscription/invoice/transaction tables ARE the
  platform ledger.
"""
import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from models.billing import (
    PlatformSubscription, SubscriptionInvoice, SubscriptionStatus,
    PlatformSubscriptionResponse, SubscriptionInvoiceResponse,
    SubscriptionMetrics, BillingConfiguration, BillingPlan
)
from models.school import AcademicTerm, School
from models.student import Student, StudentStatus
from models.payment import OnlineTransaction, TransactionStatus, TransactionType
from services.paystack_service import PaystackService
from services.sms_service import sms_service

logger = logging.getLogger(__name__)


def subscription_outstanding(subscription: PlatformSubscription) -> float:
    """The single source of truth for what a school still owes on a
    subscription: base amount plus late fees plus mid-term proration charges,
    minus discounts and payments."""
    grand_total = (
        subscription.total_amount_due
        + subscription.late_fee_amount
        + subscription.proration_adjustment
        - subscription.discount_amount
    )
    return round(grand_total - subscription.amount_paid, 2)


async def mark_transaction_refunded(
    session: AsyncSession,
    transaction_id: str,
    refunded_by: str,
    notes: Optional[str] = None,
    amount: Optional[float] = None,
) -> Dict:
    """Mark a flagged-for-refund transaction as actually refunded.

    Works for both FEE and SUBSCRIPTION transactions — refund tracking
    lives on OnlineTransaction regardless of type. No live Paystack refund
    API call is made; this just closes out the manual refund queue entry,
    same manual-reconciliation pattern as bank reconciliation elsewhere.
    """
    result = await session.execute(
        select(OnlineTransaction).where(OnlineTransaction.id == transaction_id)
    )
    transaction = result.scalar_one_or_none()

    if not transaction:
        return {"success": False, "error": "Transaction not found"}

    if transaction.refund_status != "pending":
        return {"success": False, "error": f"No pending refund on this transaction (status: {transaction.refund_status})"}

    transaction.refund_status = "completed"
    transaction.refunded_at = datetime.utcnow()
    transaction.refunded_by = refunded_by
    transaction.refund_notes = notes
    if amount is not None:
        transaction.refund_amount = round(amount, 2)
    transaction.updated_at = datetime.utcnow()

    # Post the refund actually leaving the account — but ONLY for a school
    # FEE transaction. A SUBSCRIPTION (platform billing) refund must never
    # touch a school's own GL — this module's own rule above is "no writes
    # to the school's GL: platform fees are Campusio revenue, not school
    # revenue." Previously this whole function posted nothing either way;
    # a fee refund left revenue permanently overstated by the refunded
    # amount with no GL reversal at all.
    refund_journal_entry_id = None
    if transaction.transaction_type == TransactionType.FEE and transaction.refund_amount:
        try:
            from services import fee_gl_service
            refund_journal_entry_id = await fee_gl_service.post_refund_payout(
                session, transaction.school_id, transaction, transaction.refund_amount, cash_account_code="1040",
            )
            transaction.refund_payout_journal_entry_id = refund_journal_entry_id
        except Exception as e:
            logger.error(f"Error posting refund payout journal entry for transaction {transaction.id}: {str(e)}")

    await session.commit()

    return {
        "success": True,
        "transaction_id": transaction.id,
        "refund_amount": transaction.refund_amount,
        "journal_entry_id": refund_journal_entry_id,
        "message": "Refund marked as completed",
    }


class PlatformBillingService:
    """Manages school platform subscription billing"""
    
    def __init__(self, paystack_secret_key: str):
        self.paystack = PaystackService(paystack_secret_key)

    async def _get_billing_config(
        self, session: AsyncSession, school_id: str
    ) -> BillingConfiguration:
        """Per-school pricing/plan; creates the default config on first use.

        Defaults: GHS 400/student/term (termly plan) or GHS 150/student/month.
        Pilot schools and negotiated discounts are handled by editing this row.
        """
        result = await session.execute(
            select(BillingConfiguration).where(
                BillingConfiguration.school_id == school_id
            )
        )
        config = result.scalar_one_or_none()
        if not config:
            config = BillingConfiguration(school_id=school_id)
            session.add(config)
            await session.flush()
        return config


    async def generate_term_subscription(
        self,
        session: AsyncSession,
        school_id: str,
        academic_term_id: str
    ) -> Dict:
        """
        Generate platform subscription for a term
        
        Called at the start of each academic term
        
        Returns:
        {
            "success": True,
            "subscription_id": "sub-xxx",
            "invoice_id": "inv-xxx",
            "total_due": 8400.00
        }
        """
        try:
            config = await self._get_billing_config(session, school_id)
            plan = BillingPlan(config.billing_plan) if isinstance(config.billing_plan, str) else config.billing_plan
            billing_month = datetime.utcnow().strftime("%Y-%m") if plan == BillingPlan.MONTHLY else None

            # Uniqueness: termly plans bill once per term; monthly plans bill once
            # per calendar month (multiple subscriptions per term are expected).
            if plan == BillingPlan.MONTHLY:
                existing = await session.execute(
                    select(PlatformSubscription).where(
                        PlatformSubscription.school_id == school_id,
                        PlatformSubscription.billing_month == billing_month
                    )
                )
                duplicate_error = f"Subscription already exists for {billing_month}"
            else:
                existing = await session.execute(
                    select(PlatformSubscription).where(
                        PlatformSubscription.school_id == school_id,
                        PlatformSubscription.academic_term_id == academic_term_id
                    )
                )
                duplicate_error = "Subscription already exists for this term"
            if existing.scalars().first():
                return {"success": False, "error": duplicate_error}

            # Get academic term details
            term_result = await session.execute(
                select(AcademicTerm).where(AcademicTerm.id == academic_term_id)
            )
            academic_term = term_result.scalar_one_or_none()
            if not academic_term:
                return {"success": False, "error": "Academic term not found"}

            # Count active students for this school
            students_result = await session.execute(
                select(Student).where(
                    Student.school_id == school_id,
                    Student.status == StudentStatus.ACTIVE
                )
            )
            active_students = students_result.scalars().all()
            student_count = len(active_students)

            if student_count == 0:
                return {
                    "success": False,
                    "error": "No active students in school"
                }

            # Calculate subscription amount from the school's configured plan
            unit_price = config.monthly_unit_price if plan == BillingPlan.MONTHLY else config.unit_price
            total_due = round(student_count * unit_price, 2)
            due_date = datetime.utcnow() + timedelta(days=30)

            # Apply any bulk discount rule the school qualifies for (e.g.
            # "500+ students get 5% off"). This used to be entirely dead —
            # DiscountRule rows could be created and listed via the API, but
            # nothing ever called BulkDiscountService.calculate_discount, so
            # every subscription charged full price regardless of negotiated
            # discounts.
            from services.bulk_discount_service import BulkDiscountService
            discount_result = await BulkDiscountService().calculate_discount(
                session, school_id, total_due, student_count
            )
            discount_amount = discount_result.get("discount_amount", 0.0)
            discount_percentage = discount_result.get("discount_percentage", 0.0)
            discount_reason = discount_result.get("reason")
            after_discount = round(total_due - discount_amount, 2)

            # Create subscription record — a write-once billing snapshot.
            # final_amount_due starts equal to after_discount; late fees
            # adjust it later (see late_fee_service.py).
            subscription = PlatformSubscription(
                school_id=school_id,
                academic_term_id=academic_term_id,
                student_count=student_count,
                unit_price=unit_price,
                total_amount_due=total_due,
                subtotal=total_due,
                discount_amount=discount_amount,
                discount_percentage=discount_percentage,
                discount_reason=discount_reason if discount_amount > 0 else None,
                after_discount=after_discount,
                final_amount_due=after_discount,
                billing_plan=plan,
                billing_month=billing_month,
                due_date=due_date,
                status=SubscriptionStatus.PENDING,
                reconciled_student_count=student_count,
            )

            session.add(subscription)
            try:
                await session.flush()
            except IntegrityError:
                # Partial unique index hit: a concurrent request created the
                # same term/month subscription between our check and this
                # insert. The check above catches the sequential case; this
                # catches the race.
                await session.rollback()
                return {"success": False, "error": duplicate_error}

            # Create invoice. invoice_number is globally unique; retry with a
            # recount if a concurrent generation for another school grabbed
            # the same sequence number.
            invoice = None
            for attempt in range(3):
                invoice_number = await self._generate_invoice_number(session, offset=attempt)
                invoice = SubscriptionInvoice(
                    school_id=school_id,
                    subscription_id=subscription.id,
                    invoice_number=invoice_number,
                    academic_year=academic_term.academic_year,
                    term=academic_term.term.value,
                    student_count=student_count,
                    unit_price=unit_price,
                    subtotal=total_due,
                    total_amount=total_due,
                    due_date=due_date,
                    status="ISSUED"
                )
                session.add(invoice)
                subscription.invoice_id = invoice.id
                try:
                    await session.flush()
                    break
                except IntegrityError:
                    await session.rollback()
                    # The subscription insert rolled back too - redo it
                    session.add(subscription)
                    await session.flush()
                    invoice = None
            if invoice is None:
                await session.rollback()
                return {"success": False, "error": "Could not allocate a unique invoice number, please retry"}

            await session.commit()

            logger.info(
                f"Generated subscription for school {school_id}, "
                f"term {academic_term_id}, amount: GHS {total_due}"
            )

            return {
                "success": True,
                "subscription_id": subscription.id,
                "invoice_id": invoice.id,
                "student_count": student_count,
                "total_due": total_due,
                "discount_amount": discount_amount,
                "discount_percentage": discount_percentage,
                "final_amount_due": after_discount,
                "due_date": due_date.isoformat()
            }

        except Exception as e:
            logger.error(f"Error generating subscription: {str(e)}")
            await session.rollback()
            return {"success": False, "error": str(e)}
    
    async def initiate_subscription_payment(
        self,
        session: AsyncSession,
        subscription_id: str,
        payer_email: str,
        amount_to_pay: Optional[float] = None
    ) -> Dict:
        """
        Initiate Paystack payment for subscription
        
        Returns:
        {
            "success": True,
            "payment_url": "https://checkout.paystack.com/...",
            "transaction_id": "txn-xxx",
            "reference": "PLAT-xxx"
        }
        """
        try:
            # Get subscription
            sub_result = await session.execute(
                select(PlatformSubscription).where(
                    PlatformSubscription.id == subscription_id
                )
            )
            subscription = sub_result.scalar_one_or_none()
            
            if not subscription:
                return {"success": False, "error": "Subscription not found"}
            
            if subscription.status == SubscriptionStatus.CANCELLED:
                return {"success": False, "error": "Subscription is cancelled"}

            # Calculate amount to pay — includes late fees and discounts, not
            # just the base amount, so late fees are actually collectible.
            amount_due = subscription_outstanding(subscription)

            if amount_due <= 0:
                return {"success": False, "error": "No amount due"}
            
            if amount_to_pay is not None:
                if amount_to_pay <= 0:
                    return {
                        "success": False,
                        "error": "Payment amount must be greater than zero"
                    }
                if amount_to_pay > amount_due:
                    return {
                        "success": False,
                        "error": f"Payment exceeds balance of GHS {amount_due}"
                    }
                payment_amount = amount_to_pay
            else:
                payment_amount = amount_due

            # Idempotency: a double-click on "Pay Now", or a retry after a
            # slow response, must not spin up a second Paystack checkout
            # session for the same subscription — same guard as
            # check_duplicate_pending_transaction for school-fee checkout.
            existing_result = await session.execute(
                select(OnlineTransaction).where(
                    OnlineTransaction.fee_id == subscription_id,
                    OnlineTransaction.transaction_type == TransactionType.SUBSCRIPTION,
                    OnlineTransaction.status == TransactionStatus.PENDING,
                    OnlineTransaction.amount >= payment_amount - 0.01,
                    OnlineTransaction.amount <= payment_amount + 0.01,
                ).order_by(OnlineTransaction.initiated_at.desc())
            )
            existing = existing_result.scalars().first()
            if existing:
                logger.info(f"Duplicate subscription payment init blocked for {subscription_id}, reusing {existing.reference}")
                return {
                    "success": True,
                    "transaction_id": existing.id,
                    "payment_url": existing.payment_url,
                    "reference": existing.reference,
                    "amount": existing.amount,
                    "duplicate": True,
                }

            # Create transaction record
            transaction_id = f"PLAT-{uuid.uuid4().hex[:12].upper()}"
            
            transaction = OnlineTransaction(
                school_id=subscription.school_id,
                fee_id=subscription_id,  # Using subscription ID as fee_id
                student_id="",  # Not applicable for platform billing
                parent_id="",  # Not applicable
                amount=payment_amount,
                gateway="paystack",
                reference=transaction_id,
                transaction_type=TransactionType.SUBSCRIPTION,  # Mark as subscription, not fee
                status=TransactionStatus.PENDING
            )
            
            session.add(transaction)
            await session.flush()
            
            # Call Paystack
            amount_kobo = int(payment_amount * 100)
            metadata = {
                "type": "platform_subscription",
                "subscription_id": subscription_id,
                "school_id": subscription.school_id,
                "invoice_id": subscription.invoice_id
            }
            
            paystack_result = await self.paystack.initialize_payment(
                amount_kobo=amount_kobo,
                email=payer_email,
                reference=transaction_id,
                metadata=metadata
            )
            
            logger.info(f"Paystack result: {paystack_result}")
            
            if not paystack_result.get("success"):
                await session.rollback()
                error_msg = paystack_result.get("error", "Paystack initialization failed")
                logger.error(f"Paystack init failed: {error_msg}, result: {paystack_result}")
                return {
                    "success": False,
                    "error": error_msg
                }
            
            # Store Paystack reference
            transaction.status = TransactionStatus.PROCESSING
            transaction.reference = paystack_result["reference"]
            transaction.access_code = paystack_result.get("access_code")
            transaction.payment_url = paystack_result["authorization_url"]
            
            subscription.online_transaction_id = transaction.id
            
            await session.commit()
            
            return {
                "success": True,
                "transaction_id": transaction.id,
                "payment_url": transaction.payment_url,
                "reference": transaction.reference,
                "amount": payment_amount
            }
            
        except Exception as e:
            logger.error(f"Error initiating payment: {str(e)}")
            await session.rollback()
            return {"success": False, "error": str(e)}
    
    async def verify_and_process_payment(
        self,
        session: AsyncSession,
        transaction_id: str,
        reference: str,
        amount_paid: float
    ) -> Dict:
        """
        Verify payment and process subscription

        Called from the Paystack webhook (and the manual verify endpoint)
        after Paystack confirms payment.

        Idempotent: Paystack retries webhooks until acknowledged, and the
        manual verify endpoint can race the webhook. The transaction row is
        the idempotency key — locked FOR UPDATE, and once its status is
        SUCCESS the same money is never applied again.
        """
        try:
            # Lock the transaction row for the duration of the application so
            # a concurrent webhook retry / manual verify blocks here and then
            # sees status=SUCCESS instead of double-applying.
            txn_result = await session.execute(
                select(OnlineTransaction)
                .where(OnlineTransaction.id == transaction_id)
                .with_for_update()
            )
            transaction = txn_result.scalar_one_or_none()

            if not transaction:
                return {"success": False, "error": "Transaction not found"}

            if transaction.status == TransactionStatus.SUCCESS:
                logger.info(f"Subscription payment already processed, skipping: {reference}")
                return {
                    "success": True,
                    "already_processed": True,
                    "subscription_id": transaction.fee_id,
                }

            # Get subscription
            sub_result = await session.execute(
                select(PlatformSubscription).where(
                    PlatformSubscription.id == transaction.fee_id
                )
            )
            subscription = sub_result.scalar_one_or_none()

            if not subscription:
                return {"success": False, "error": "Subscription not found"}

            remaining_due = subscription_outstanding(subscription)

            transaction.status = TransactionStatus.SUCCESS
            transaction.amount_paid = amount_paid
            transaction.completed_at = datetime.utcnow()

            if remaining_due <= 0:
                # The subscription was already fully settled (e.g. the school
                # paid two outstanding checkout links). The money genuinely
                # arrived, so the transaction is SUCCESS — but it must NOT be
                # applied to the subscription again. Flag it for a refund
                # instead of silently absorbing it.
                logger.warning(
                    f"OVERPAYMENT: {reference} paid GHS {amount_paid} against "
                    f"already-settled subscription {subscription.id} — needs refund"
                )
                transaction.failed_reason = "Overpayment: subscription already settled, refund required"
                transaction.refund_status = "pending"
                transaction.refund_amount = round(amount_paid, 2)
                await session.commit()
                return {
                    "success": True,
                    "overpayment": True,
                    "subscription_id": subscription.id,
                    "amount_paid": amount_paid,
                    "message": "Subscription already settled; payment flagged for refund",
                }

            # Never apply more than what's owed to the ledger.
            amount_applied = round(min(amount_paid, remaining_due), 2)
            subscription.amount_paid = round(subscription.amount_paid + amount_applied, 2)
            subscription.status = SubscriptionStatus.ACTIVE
            subscription.updated_at = datetime.utcnow()
            if subscription_outstanding(subscription) <= 0.01:
                subscription.paid_at = datetime.utcnow()
            if amount_applied < amount_paid:
                logger.warning(
                    f"OVERPAYMENT: {reference} paid GHS {amount_paid}, only "
                    f"GHS {amount_applied} was owed — excess needs refund"
                )
                transaction.failed_reason = (
                    f"Overpayment: GHS {round(amount_paid - amount_applied, 2)} excess, refund required"
                )
                transaction.refund_status = "pending"
                transaction.refund_amount = round(amount_paid - amount_applied, 2)

            # Note: no GL journal entry is written here. Platform fees are
            # Campusio revenue, not the school's — the subscription, invoice,
            # and transaction rows are the platform ledger.

            # Update invoice status
            if subscription.invoice_id:
                inv_result = await session.execute(
                    select(SubscriptionInvoice).where(
                        SubscriptionInvoice.id == subscription.invoice_id
                    )
                )
                invoice = inv_result.scalar_one_or_none()
                if invoice:
                    invoice.amount_paid = round(float(invoice.amount_paid) + amount_applied, 2)
                    remaining_balance = float(invoice.total_amount) - invoice.amount_paid

                    if remaining_balance <= 0.01:  # Essentially fully paid
                        invoice.status = "PAID"
                        invoice.paid_at = datetime.utcnow()
                        logger.info(f"Invoice {invoice.invoice_number} marked as PAID")
                    else:
                        invoice.status = "PARTIAL"
                        logger.info(
                            f"Invoice {invoice.invoice_number} marked as PARTIAL "
                            f"(remaining: GHS {remaining_balance:.2f})"
                        )

            await session.commit()

            logger.info(
                f"Processed payment for subscription {subscription.id}, "
                f"amount applied: GHS {amount_applied}"
            )

            return {
                "success": True,
                "subscription_id": subscription.id,
                "status": subscription.status.value,
                "amount_paid": amount_applied
            }

        except Exception as e:
            logger.error(f"Error processing payment: {str(e)}")
            await session.rollback()
            return {"success": False, "error": str(e)}
    
    async def process_auto_renewal(self, session: AsyncSession, subscription: PlatformSubscription) -> Dict:
        """Attempt to auto-charge a school's saved card for a due/overdue
        subscription. Called from services/scheduler.py's daily job — never
        raises, only reports what happened, so one school's failed card
        never stops the run for the rest. Falls through to the existing
        manual-checkout / reminder / suspension flow whenever there's no
        saved card, auto-renew is off, or the charge is declined."""
        try:
            amount_due = subscription_outstanding(subscription)
            if amount_due <= 0:
                return {"success": True, "skipped": "nothing owed"}

            config_result = await session.execute(
                select(BillingConfiguration).where(BillingConfiguration.school_id == subscription.school_id)
            )
            config = config_result.scalar_one_or_none()
            if not config or not config.auto_renew_enabled or not config.paystack_authorization_code:
                return {"success": True, "skipped": "no saved card or auto-renew disabled"}

            school_result = await session.execute(select(School).where(School.id == subscription.school_id))
            school = school_result.scalar_one_or_none()
            if not school or not school.email:
                return {"success": True, "skipped": "school has no billing email on file"}

            # Same idempotency guard as initiate_subscription_payment — don't
            # start a second attempt while one is already in flight.
            existing_result = await session.execute(
                select(OnlineTransaction).where(
                    OnlineTransaction.fee_id == subscription.id,
                    OnlineTransaction.transaction_type == TransactionType.SUBSCRIPTION,
                    OnlineTransaction.status.in_([TransactionStatus.PENDING, TransactionStatus.PROCESSING]),
                )
            )
            if existing_result.scalars().first():
                return {"success": True, "skipped": "payment already in flight"}

            transaction_id = f"PLAT-{uuid.uuid4().hex[:12].upper()}"
            transaction = OnlineTransaction(
                school_id=subscription.school_id,
                fee_id=subscription.id,
                student_id="",
                parent_id="",
                amount=amount_due,
                gateway="paystack",
                reference=transaction_id,
                transaction_type=TransactionType.SUBSCRIPTION,
                status=TransactionStatus.PENDING,
            )
            session.add(transaction)
            await session.flush()

            charge_result = await self.paystack.charge_authorization(
                authorization_code=config.paystack_authorization_code,
                amount_kobo=int(round(amount_due * 100)),
                email=school.email,
                reference=transaction_id,
                metadata={
                    "type": "platform_subscription_auto_renewal",
                    "subscription_id": subscription.id,
                    "school_id": subscription.school_id,
                },
            )

            if not charge_result.get("success"):
                transaction.status = TransactionStatus.FAILED
                transaction.failed_reason = charge_result.get("error", "Auto-charge request failed")
                await session.commit()
                logger.warning(f"Auto-renewal charge request failed for subscription {subscription.id}: {charge_result.get('error')}")
                return {"success": False, "error": charge_result.get("error")}

            if charge_result.get("status") != "success":
                gateway_msg = (charge_result.get("data") or {}).get("gateway_response", "declined")
                transaction.status = TransactionStatus.FAILED
                transaction.failed_reason = f"Card declined: {gateway_msg}"
                await session.commit()
                logger.warning(f"Auto-renewal charge declined for subscription {subscription.id}: {gateway_msg}")
                return {"success": False, "error": transaction.failed_reason}

            # Charged successfully on Paystack's side — apply it to the
            # ledger the same way the webhook would. The webhook will also
            # fire for this same reference; verify_and_process_payment's
            # row-lock + status check makes applying it twice a no-op.
            await session.commit()

            apply_result = await self.verify_and_process_payment(
                session=session,
                transaction_id=transaction.id,
                reference=transaction_id,
                amount_paid=amount_due,
            )
            logger.info(f"Auto-renewal charged and applied for subscription {subscription.id}: GHS {amount_due}")
            return apply_result

        except Exception as e:
            logger.error(f"Error processing auto-renewal for subscription {subscription.id}: {str(e)}")
            await session.rollback()
            return {"success": False, "error": str(e)}

    @staticmethod
    def _to_response(sub: PlatformSubscription) -> PlatformSubscriptionResponse:
        return PlatformSubscriptionResponse(
            id=sub.id,
            school_id=sub.school_id,
            academic_term_id=sub.academic_term_id,
            student_count=sub.student_count,
            unit_price=sub.unit_price,
            total_amount_due=sub.total_amount_due,
            amount_paid=sub.amount_paid,
            proration_adjustment=sub.proration_adjustment,
            outstanding_balance=subscription_outstanding(sub),
            status=sub.status,
            billing_plan=sub.billing_plan if isinstance(sub.billing_plan, str) else sub.billing_plan.value,
            billing_month=sub.billing_month,
            billing_date=sub.billing_date,
            due_date=sub.due_date,
            paid_at=sub.paid_at,
            created_at=sub.created_at
        )

    async def get_subscription(
        self,
        session: AsyncSession,
        subscription_id: str
    ) -> Optional[PlatformSubscriptionResponse]:
        """Get subscription details"""
        result = await session.execute(
            select(PlatformSubscription).where(
                PlatformSubscription.id == subscription_id
            )
        )
        sub = result.scalar_one_or_none()

        if not sub:
            return None

        return self._to_response(sub)

    async def get_school_current_subscription(
        self,
        session: AsyncSession,
        school_id: str
    ) -> Optional[PlatformSubscriptionResponse]:
        """Get current term subscription for school"""
        result = await session.execute(
            select(PlatformSubscription)
            .where(PlatformSubscription.school_id == school_id)
            .order_by(PlatformSubscription.created_at.desc())
        )
        sub = result.scalars().first()

        if not sub:
            return None

        return self._to_response(sub)

    async def get_school_subscriptions(
        self,
        session: AsyncSession,
        school_id: str,
        limit: int = 10,
        offset: int = 0
    ) -> List[PlatformSubscriptionResponse]:
        """Get all subscriptions for school"""
        result = await session.execute(
            select(PlatformSubscription)
            .where(PlatformSubscription.school_id == school_id)
            .order_by(PlatformSubscription.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        subs = result.scalars().all()

        return [self._to_response(sub) for sub in subs]
    
    async def get_school_invoices(
        self,
        session: AsyncSession,
        school_id: str,
        limit: int = 10,
        offset: int = 0
    ) -> List[SubscriptionInvoiceResponse]:
        """Get all invoices for school"""
        result = await session.execute(
            select(SubscriptionInvoice)
            .where(SubscriptionInvoice.school_id == school_id)
            .order_by(SubscriptionInvoice.issued_at.desc())
            .limit(limit)
            .offset(offset)
        )
        invoices = result.scalars().all()
        
        return [
            SubscriptionInvoiceResponse(
                id=inv.id,
                school_id=inv.school_id,
                invoice_number=inv.invoice_number,
                academic_year=inv.academic_year,
                term=inv.term,
                student_count=inv.student_count,
                total_amount=inv.total_amount,
                amount_paid=inv.amount_paid,
                status=inv.status,
                issued_at=inv.issued_at,
                due_date=inv.due_date,
                paid_at=inv.paid_at
            )
            for inv in invoices
        ]
    
    async def get_subscription_metrics(
        self,
        session: AsyncSession,
        school_id: str
    ) -> Optional[SubscriptionMetrics]:
        """Get subscription metrics for dashboard"""
        # Fetch the raw row (not the Response wrapper) — subscription_outstanding()
        # needs late_fee_amount/discount_amount, which PlatformSubscriptionResponse
        # doesn't carry.
        result = await session.execute(
            select(PlatformSubscription)
            .where(PlatformSubscription.school_id == school_id)
            .order_by(PlatformSubscription.created_at.desc())
        )
        current = result.scalars().first()

        if not current:
            return None

        days_until_due = (current.due_date - datetime.utcnow()).days
        # remaining_balance/is_overdue must agree with subscription_outstanding() —
        # the figure actually used for payment, suspension, and reactivation.
        # total_amount_due - amount_paid alone ignores late fees and discounts,
        # so the dashboard could show a different balance than what's collectible.
        remaining_balance = subscription_outstanding(current)
        is_overdue = days_until_due < 0 and remaining_balance > 0

        return SubscriptionMetrics(
            school_id=school_id,
            current_term_status=current.status,
            total_due=current.total_amount_due,
            total_paid=current.amount_paid,
            remaining_balance=remaining_balance,
            student_count=current.student_count,
            unit_price=current.unit_price,
            days_until_due=max(days_until_due, 0),
            is_overdue=is_overdue
        )
    
    # ========================================================================
    # PRIVATE HELPER METHODS
    # ========================================================================
    
    async def _generate_invoice_number(
        self,
        session: AsyncSession,
        offset: int = 0
    ) -> str:
        """Generate the next invoice number, e.g. PLAT-2026-0001.

        invoice_number is globally unique (DB constraint), so the sequence is
        counted across ALL schools — the old per-school count handed every
        school's first invoice the same number and the second school's
        generation crashed on the unique constraint. The LIKE pattern also
        used to match against the bare year ("2026%") which never matched
        numbers starting with "PLAT-", so the count was permanently zero.

        `offset` lets the caller retry with the next number if a concurrent
        generation won the race for this one.
        """
        year = datetime.utcnow().year
        result = await session.execute(
            select(SubscriptionInvoice.id).where(
                SubscriptionInvoice.invoice_number.like(f"PLAT-{year}-%")
            )
        )
        count = len(result.all())
        return f"PLAT-{year}-{str(count + 1 + offset).zfill(4)}"

