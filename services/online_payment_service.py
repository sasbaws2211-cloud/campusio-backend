"""High-level online payment service - orchestrates payment flow"""
import logging
import uuid
from datetime import datetime, timedelta
from typing import Dict, Optional
from fastapi import BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import and_
from sqlmodel import select

from models.fee import Fee, FeePayment, FeeStructure, PaymentStatus, PaymentMethod, FeeType
from models.school import School
from models.student import Parent, Student
from models.payment import OnlineTransaction, TransactionStatus, PaymentVerification, TransactionType
from models.finance import JournalEntry, JournalLineItem, ReferenceType, JournalEntryCreate, JournalLineItemCreate
from models.finance.chart_of_accounts import GLAccount
from services.paystack_service import PaystackService
from services.sms_service import sms_service  # Existing SMS service
from services.journal_entry_service import JournalEntryService
from services.canteen_wallet_service import CanteenWalletService
from services.extra_class_service import apply_billing_payment
from services.receipt_sequence_service import get_next_receipt_number

logger = logging.getLogger(__name__)


class OnlinePaymentService:
    """Orchestrates online payment flow"""
    
    def __init__(self, paystack_secret_key: str):
        self.paystack = PaystackService(paystack_secret_key)

    async def _get_school_subaccount(self, session: AsyncSession, school_id: str) -> Optional[str]:
        """A verified school's Paystack subaccount code, so a fee payment splits and
        settles straight to the school's own bank/MoMo account instead of the
        platform's pooled main balance. None for schools that haven't been
        verified for direct settlement — those payments fall back to the
        platform's main balance, same as before this existed."""
        result = await session.execute(select(School).where(School.id == school_id))
        school = result.scalar_one_or_none()
        return school.paystack_subaccount_code if school else None

    async def initiate_payment(
        self,
        session: AsyncSession,
        fee_id: str,
        parent_id: str,  # NOTE: This is User.id, not Parent.id. Will be looked up via Parent.user_id
        parent_email: str,
        school_id: str,
        amount_to_pay: Optional[float] = None,
        fee: Optional[Fee] = None  # Optional: pre-fetched fee object for validation
    ) -> Dict:
        """
        Initiate online payment for a fee
        
        SECURITY: This method validates the fee object to prevent unauthorized payments
        
        Args:
            parent_id: User.id of the parent user (not Parent.id)
                       Will be looked up via Parent.user_id relationship
        
        Returns:
        {
            "success": True,
            "transaction_id": "txn-xxx",
            "payment_url": "https://checkout.paystack.com/...",
            "reference": "PAY-xxx",
            "amount": 500.00
        }
        """
        
        try:
            # Get fee details if not provided
            if fee is None:
                fee_result = await session.execute(
                    select(Fee).where(Fee.id == fee_id)
                )
                fee = fee_result.scalar_one_or_none()
            
            if not fee:
                return {"success": False, "error": "Fee not found"}
            
            # SECURITY: Validate fee belongs to correct school (defensive check)
            if fee.school_id != school_id:
                logger.error(
                    f"School mismatch in payment: fee.school_id={fee.school_id}, "
                    f"expected={school_id}, parent_id={parent_id}"
                )
                return {"success": False, "error": "Unauthorized fee access"}
            
            # SECURITY: Validate parent exists (defensive check)
            # Note: parent_id is actually User.id, so we query by user_id
            parent_result = await session.execute(
                select(Parent).where(Parent.user_id == parent_id)
            )
            parent_record = parent_result.scalar_one_or_none()
            if not parent_record:
                logger.error(f"Parent record not found: {parent_id}")
                return {"success": False, "error": "Parent not found"}
            
            # Calculate amount due (total - already paid - discount)
            amount_due = fee.amount_due - fee.amount_paid - (fee.discount or 0)
            if amount_due <= 0:
                return {"success": False, "error": "No amount due"}

            # Use custom amount if provided, otherwise use full balance
            if amount_to_pay is not None:
                if amount_to_pay <= 0:
                    return {"success": False, "error": "Payment amount must be greater than zero"}
                if amount_to_pay > amount_due:
                    return {"success": False, "error": f"Payment amount cannot exceed outstanding balance of GHS {amount_due}"}
                payment_amount = amount_to_pay
            else:
                payment_amount = amount_due

            # Create transaction record
            transaction_id = f"TXN-{uuid.uuid4().hex[:12].upper()}"
            
            # Use the actual Parent.id (not User.id) for transaction record
            transaction = OnlineTransaction(
                school_id=school_id,
                fee_id=fee_id,
                student_id=fee.student_id,
                parent_id=parent_record.id,  # Use Parent record ID, not User ID
                amount=payment_amount,
                gateway="paystack",
                reference=transaction_id,
                transaction_type=TransactionType.FEE,  # Mark as fee payment
                status=TransactionStatus.PENDING
            )
            
            session.add(transaction)
            await session.flush()  # Get the ID
            
            # Call Paystack API
            amount_kobo = int(payment_amount * 100)  # Convert GHS to kobo
            metadata = {
                "fee_id": fee_id,
                "student_id": fee.student_id,
                "transaction_id": transaction_id,
                "is_partial": amount_to_pay is not None
            }
            
            subaccount = await self._get_school_subaccount(session, school_id)
            paystack_result = await self.paystack.initialize_payment(
                amount_kobo=amount_kobo,
                email=parent_email,
                reference=transaction_id,
                metadata=metadata,
                subaccount=subaccount,
            )
            
            if paystack_result["success"]:
                # Update transaction with Paystack details
                transaction.payment_url = paystack_result["authorization_url"]
                transaction.access_code = paystack_result["access_code"]
                transaction.reference = paystack_result["reference"]
                transaction.status = TransactionStatus.PROCESSING
                
                session.add(transaction)
                await session.commit()
                
                logger.info(f"Payment initiated: {transaction_id}")
                
                return {
                    "success": True,
                    "transaction_id": str(transaction.id),  # Return UUID, not reference
                    "payment_url": paystack_result["authorization_url"],
                    "reference": paystack_result["reference"],
                    "amount": payment_amount
                }
            else:
                transaction.status = TransactionStatus.FAILED
                transaction.failed_reason = paystack_result.get("error", "Payment initialization failed")
                session.add(transaction)
                await session.commit()
                
                return {
                    "success": False,
                    "error": paystack_result.get("error", "Payment initialization failed")
                }
        
        except Exception as e:
            logger.error(f"Error initiating payment: {str(e)}")
            return {
                "success": False,
                "error": f"Error: {str(e)}"
            }
    
    async def request_momo_payment(
        self,
        session: AsyncSession,
        fee_id: str,
        school_id: str,
        provider: str,
        phone: Optional[str] = None,
        amount_to_pay: Optional[float] = None,
    ) -> Dict:
        """School-initiated mobile-money payment request.

        Sends a MoMo approval prompt to the parent's registered phone (or an
        explicit phone), so parents without smartphones can pay by approving
        on any handset. Confirmation flows through the same Paystack webhook
        as every other payment, so GL posting and receipts are identical.
        """
        try:
            fee_result = await session.execute(select(Fee).where(Fee.id == fee_id))
            fee = fee_result.scalar_one_or_none()
            if not fee:
                return {"success": False, "error": "Fee not found"}
            if fee.school_id != school_id:
                return {"success": False, "error": "Unauthorized fee access"}

            amount_due = fee.amount_due - fee.amount_paid - (fee.discount or 0)
            if amount_due <= 0:
                return {"success": False, "error": "No outstanding balance on this fee"}

            if amount_to_pay is not None:
                if amount_to_pay <= 0:
                    return {"success": False, "error": "Payment amount must be greater than zero"}
                if amount_to_pay > amount_due:
                    return {"success": False, "error": f"Amount cannot exceed outstanding balance of GHS {amount_due:.2f}"}
                payment_amount = amount_to_pay
            else:
                payment_amount = amount_due

            # Idempotency: block a duplicate MoMo prompt for this fee while an
            # earlier one is still in flight (double-click, or the admin
            # retrying because they didn't see a response) — otherwise the
            # parent gets two approval prompts and, if they approve both,
            # two separate Paystack references both succeed since each is
            # its own idempotency key. Mirrors check_duplicate_pending_transaction
            # used for parent-initiated checkout in routers/payments.py.
            recent_cutoff = datetime.utcnow() - timedelta(minutes=10)
            existing_result = await session.execute(
                select(OnlineTransaction).where(
                    OnlineTransaction.fee_id == fee_id,
                    OnlineTransaction.transaction_type == TransactionType.FEE,
                    OnlineTransaction.status.in_([TransactionStatus.PENDING, TransactionStatus.PROCESSING]),
                    OnlineTransaction.initiated_at >= recent_cutoff,
                ).order_by(OnlineTransaction.initiated_at.desc())
            )
            existing = existing_result.scalars().first()
            if existing:
                logger.info(f"Duplicate MoMo request blocked for fee {fee_id}, reusing transaction {existing.reference}")
                return {
                    "success": True,
                    "transaction_id": str(existing.id),
                    "reference": existing.reference,
                    "amount": existing.amount,
                    "message": "A payment request is already pending for this fee — waiting for the parent to approve.",
                    "duplicate": True,
                }

            # Find the student's parent for the wallet number + receipt email
            from models.student import StudentParent
            link_result = await session.execute(
                select(StudentParent).where(StudentParent.student_id == fee.student_id)
            )
            link = link_result.scalars().first()
            if not link:
                return {"success": False, "error": "No parent linked to this student"}
            parent_result = await session.execute(
                select(Parent).where(Parent.id == link.parent_id)
            )
            parent_record = parent_result.scalar_one_or_none()
            if not parent_record:
                return {"success": False, "error": "Parent record not found"}

            momo_phone = (phone or parent_record.phone or "").strip()
            if not momo_phone:
                return {"success": False, "error": "No phone number on record — enter one to send the prompt to"}
            parent_email = parent_record.email or f"parent-{parent_record.id}@campusio.online"

            transaction_id = f"TXN-{uuid.uuid4().hex[:12].upper()}"
            transaction = OnlineTransaction(
                school_id=school_id,
                fee_id=fee_id,
                student_id=fee.student_id,
                parent_id=parent_record.id,
                amount=payment_amount,
                gateway="paystack",
                reference=transaction_id,
                transaction_type=TransactionType.FEE,
                status=TransactionStatus.PENDING,
            )
            session.add(transaction)
            await session.flush()

            subaccount = await self._get_school_subaccount(session, school_id)
            result = await self.paystack.charge_mobile_money(
                amount_kobo=int(payment_amount * 100),
                email=parent_email,
                phone=momo_phone,
                provider=provider,
                reference=transaction_id,
                metadata={
                    "fee_id": fee_id,
                    "student_id": fee.student_id,
                    "transaction_id": transaction_id,
                    "school_initiated": True,
                },
                subaccount=subaccount,
            )

            if result["success"]:
                transaction.status = TransactionStatus.PROCESSING
                session.add(transaction)
                await session.commit()
                masked = momo_phone[:3] + "****" + momo_phone[-3:] if len(momo_phone) >= 6 else momo_phone
                return {
                    "success": True,
                    "transaction_id": str(transaction.id),
                    "reference": transaction_id,
                    "amount": payment_amount,
                    "phone": masked,
                    "message": result.get("display_text"),
                }

            transaction.status = TransactionStatus.FAILED
            transaction.failed_reason = result.get("error", "Mobile money charge failed")
            session.add(transaction)
            await session.commit()
            return {"success": False, "error": result.get("error", "Mobile money charge failed")}

        except Exception as e:
            logger.error(f"Error requesting MoMo payment: {str(e)}")
            return {"success": False, "error": f"Error: {str(e)}"}

    async def process_webhook(
        self,
        session: AsyncSession,
        payload: Dict,
        background_tasks: Optional[BackgroundTasks] = None
    ) -> Dict:
        """
        Process webhook from Paystack
        
        Paystack sends this when payment status changes
        """
        
        try:
            # Extract reference from nested structure (data is the wrapper)
            data = payload.get("data", {})
            reference = data.get("reference") if isinstance(data, dict) else payload.get("reference")
            amount = data.get("amount") if isinstance(data, dict) else payload.get("amount")
            status_val = data.get("status") if isinstance(data, dict) else payload.get("status")

            logger.info(f"Processing webhook - Reference: {reference}, Amount: {amount}, Status: {status_val}")

            if not reference:
                # No safe way to identify the transaction without a reference:
                # OnlineTransaction doesn't store a payer email, and guessing
                # by amount alone risks crediting the wrong parent's money to
                # a different transaction — worse than failing cleanly on a
                # ledger. (A previous version of this fallback referenced a
                # payer_email column that never existed on the model, so it
                # always raised AttributeError before reaching this point.)
                logger.error("Webhook missing reference - cannot safely match to a transaction")
                return {"success": False, "error": "Could not match payment to transaction"}

            # Find transaction. Locked FOR UPDATE so a concurrent webhook
            # retry (Paystack resends until it gets a 2xx) blocks here
            # instead of racing this one to also read status != SUCCESS
            # and double-apply the payment — same pattern as the platform
            # subscription path's verify_and_process_payment.
            trans_result = await session.execute(
                select(OnlineTransaction)
                .where(OnlineTransaction.reference == reference)
                .with_for_update()
            )
            transaction = trans_result.scalar_one_or_none()

            if not transaction:
                logger.warning(f"Transaction not found: {reference}")
                return {"success": False, "error": "Transaction not found"}

            # Check if already processed (idempotency)
            if transaction.status == TransactionStatus.SUCCESS:
                logger.info(f"Transaction already processed: {reference}")
                return {"success": True, "processed": False}
            
            # Check webhook status first (no API call needed)
            if status_val == "success":
                logger.info(f"Webhook indicates success, processing payment: {reference}")
                paystack_status = "success"
                amount_paid = amount / 100 if amount else 0  # Convert from kobo
                # Create paystack_data from webhook payload for consistency
                paystack_data = data if isinstance(data, dict) else {"reference": reference, "amount": amount, "status": status_val}
            else:
                logger.info(f"Webhook status not success, verifying with Paystack: {reference}")
                # Verify with Paystack for non-success webhooks or if status missing
                verify_result = await self.paystack.verify_payment(reference)
                
                if not verify_result["success"]:
                    logger.error(f"Verification failed: {reference}")
                    transaction.status = TransactionStatus.FAILED
                    transaction.failed_reason = "Verification failed"
                    session.add(transaction)
                    await session.commit()
                    return {"success": False, "error": "Verification failed"}
                
                paystack_data = verify_result["data"]
                paystack_status = paystack_data.get("status")
                amount_paid = paystack_data.get("amount", 0) / 100  # Convert from kobo
            
            # Handle payment status
            if paystack_status == "success":
                
                # Record verification
                verification = PaymentVerification(
                    transaction_id=transaction.id,
                    gateway="paystack",
                    reference=reference,
                    expected_amount=transaction.amount,
                    actual_amount=amount_paid,
                    verified=True,
                    match_status="AMOUNT_MATCH" if abs(amount_paid - transaction.amount) < 0.01 else "AMOUNT_MISMATCH"
                )
                session.add(verification)
                
                # Update transaction
                transaction.status = TransactionStatus.SUCCESS
                transaction.payment_status = "success"
                transaction.amount_paid = amount_paid
                transaction.completed_at = datetime.utcnow()
                transaction.verified_at = datetime.utcnow()
                transaction.gateway_response = str(paystack_data)
                session.add(transaction)

                # Canteen top-ups are a distinct money flow (crediting a
                # prepaid wallet, not paying down a school fee) — handled
                # here and returned early so it never touches fee
                # distribution below. Previously this was inferred from
                # fee_id == student_id and ran *after* fee distribution had
                # already consumed the same amount_paid into FeePayments,
                # double-applying a single payment to both a fee balance and
                # the canteen wallet.
                # Admission application fees are also a distinct money flow —
                # there is no Student/Fee yet, just a public Applicant — so
                # this returns early too, same reasoning as canteen top-ups
                # and extra-class billing above.
                if transaction.transaction_type == TransactionType.ADMISSION_FEE:
                    result = await self._apply_admission_fee_payment(session, transaction)
                    await session.commit()
                    logger.info("Applied admission fee payment via webhook: %s", result)
                    return {"success": True, "processed": True}

                # Admission deposits are also a distinct money flow — same
                # early-return reasoning as admission fees above, but this
                # settles an AdmissionDeposit's paid_amount, not the
                # Applicant record itself.
                if transaction.transaction_type == TransactionType.ADMISSION_DEPOSIT:
                    result = await self._apply_admission_deposit_payment(session, transaction)
                    await session.commit()
                    logger.info("Applied admission deposit payment via webhook: %s", result)
                    return {"success": True, "processed": True}

                if transaction.transaction_type == TransactionType.CANTEEN_TOPUP:
                    wallet_service = CanteenWalletService(session)
                    wallet_result = await wallet_service.apply_topup(
                        session=session,
                        transaction=transaction,
                        amount=amount_paid,
                        description="Paystack wallet top-up",
                    )
                    await session.commit()
                    logger.info("Applied canteen wallet top-up via webhook: %s", wallet_result)
                    return {"success": True, "processed": True}

                # Extra-class billing cycles are also a distinct money flow
                # (settling a teacher-led class's billing cycle, not a
                # school fee) — same early-return pattern as canteen top-ups
                # above, for the same reason: it must never also run through
                # fee distribution below.
                if transaction.transaction_type == TransactionType.EXTRA_CLASS_FEE:
                    billing_result = await apply_billing_payment(session=session, transaction=transaction)
                    await session.commit()
                    logger.info("Applied extra-class billing payment via webhook: %s", billing_result)
                    return {"success": True, "processed": True}

                # Distribute payment to fees (handles overpayment automatically)
                remaining_amount, fee_payments = await self._distribute_payment_to_fees(
                    session=session,
                    student_id=transaction.student_id,
                    school_id=transaction.school_id,
                    amount_to_distribute=amount_paid,
                    reference_number=reference,
                    received_by="online_system"
                )

                # Flush to ensure all FeePayments have IDs
                await session.flush()
                
                # Flag any undistributed excess for refund rather than
                # silently absorbing it — this money genuinely arrived but
                # the student has no outstanding fee left to apply it to.
                if remaining_amount > 0:
                    logger.warning(
                        f"OVERPAYMENT: {reference} — GHS {remaining_amount:.2f} could not be "
                        f"distributed (student has no more outstanding fees), refund required"
                    )
                    transaction.failed_reason = (
                        f"Overpayment: GHS {remaining_amount:.2f} excess, refund required"
                    )
                    transaction.refund_status = "pending"
                    transaction.refund_amount = round(remaining_amount, 2)

                    # Recognize the excess as a liability the moment it's
                    # flagged (Dr Paystack Clearing / Cr Refunds Payable) —
                    # previously this cash had ZERO GL representation until
                    # someone remembered to refund it, silently
                    # understating GL cash for that whole window even
                    # though the school's Paystack balance genuinely holds
                    # the money.
                    try:
                        from services import fee_gl_service
                        transaction.refund_liability_journal_entry_id = await fee_gl_service.post_refund_liability(
                            session, school_id, transaction, remaining_amount, cash_account_code="1040",
                        )
                    except Exception as e:
                        logger.error(f"Error posting refund liability journal entry for transaction {transaction.id}: {str(e)}")
                
                # Update transaction with first fee payment ID (for reference)
                if fee_payments:
                    transaction.journal_entry_id = fee_payments[0].id
                
                session.add(transaction)
                await session.commit()
                
                # Get primary fee for notifications
                primary_fee_result = await session.execute(
                    select(Fee).where(Fee.id == transaction.fee_id)
                )
                primary_fee = primary_fee_result.scalar_one_or_none()
                
                # Get the first fee payment for notification data
                if fee_payments:
                    first_fee_payment = fee_payments[0]
                    
                    # Send notifications (async, don't wait)
                    try:
                        await self._send_payment_notifications(
                            session, transaction, first_fee_payment, primary_fee
                        )
                    except Exception as e:
                        logger.error(f"Error sending notifications: {str(e)}")
                
                logger.info(
                    f"Payment processed successfully: {reference}, "
                    f"Total amount: GHS {amount_paid}, "
                    f"Distributed to {len(fee_payments)} fee(s)"
                )

                if background_tasks is not None:
                    from services.webhook_service import emit_event
                    await emit_event(
                        session, background_tasks, transaction.school_id, "payment.completed",
                        {
                            "transaction_id": str(transaction.id),
                            "student_id": transaction.student_id,
                            "reference": reference,
                            "amount_paid": amount_paid,
                        },
                    )

                return {"success": True, "processed": True}
            
            else:
                # Payment failed
                transaction.status = TransactionStatus.FAILED
                transaction.payment_status = paystack_status
                transaction.failed_reason = paystack_data.get("gateway_response", "Payment declined")
                transaction.gateway_response = str(paystack_data)
                session.add(transaction)
                await session.commit()
                
                logger.warning(f"Payment failed: {reference}")
                return {"success": True, "processed": True}
        
        except Exception as e:
            logger.error(f"Webhook processing error: {str(e)}")
            await session.rollback()
            return {"success": False, "error": str(e)}
    
    async def _apply_admission_fee_payment(
        self,
        session: AsyncSession,
        transaction: OnlineTransaction,
    ) -> Dict:
        """Marks the Applicant's fee paid and moves it out of INQUIRY, then emails
        the guardian a confirmation. `transaction.student_id` holds Applicant.id
        here — there's no real Student yet, this transaction type just reuses
        the column (same trick CANTEEN_TOPUP uses for fee_id)."""
        from models.admissions import Applicant, ApplicationStatus

        result = await session.execute(select(Applicant).where(Applicant.id == transaction.student_id))
        applicant = result.scalar_one_or_none()
        if not applicant:
            logger.warning(f"Admission fee webhook for unknown applicant: {transaction.student_id}")
            return {"applied": False, "reason": "applicant_not_found"}

        applicant.application_fee_paid = True
        if applicant.status == ApplicationStatus.INQUIRY:
            applicant.status = ApplicationStatus.APPLIED
        applicant.updated_at = datetime.utcnow()
        session.add(applicant)

        if applicant.guardian_email:
            try:
                school_result = await session.execute(select(School).where(School.id == applicant.school_id))
                school = school_result.scalar_one_or_none()
                from services.email_service import email_service
                await email_service.send_admission_confirmation(
                    to=applicant.guardian_email,
                    applicant_name=f"{applicant.first_name} {applicant.last_name}",
                    school_name=school.name if school else "the school",
                    fee_paid=True,
                    amount=transaction.amount_paid,
                )
            except Exception as e:
                logger.error(f"Failed to send admission confirmation email: {e}")

        return {"applied": True, "applicant_id": applicant.id}

    async def _apply_admission_deposit_payment(
        self,
        session: AsyncSession,
        transaction: OnlineTransaction,
    ) -> Dict:
        """Credits the paid amount to the AdmissionDeposit and emails the
        guardian a receipt. `transaction.fee_id` holds AdmissionDeposit.id
        and `transaction.student_id` holds Applicant.id here — same reused-
        column trick as `_apply_admission_fee_payment` above, since there's
        no real Fee/Student for a pre-enrollment applicant."""
        from models.admissions import Applicant
        from models.admissions_enterprise import AdmissionDeposit, AdmissionDepositStatus

        result = await session.execute(select(AdmissionDeposit).where(AdmissionDeposit.id == transaction.fee_id))
        deposit = result.scalar_one_or_none()
        if not deposit:
            logger.warning(f"Admission deposit webhook for unknown deposit: {transaction.fee_id}")
            return {"applied": False, "reason": "deposit_not_found"}

        deposit.paid_amount = min(deposit.paid_amount + transaction.amount_paid, deposit.required_amount)
        deposit.status = AdmissionDepositStatus.PAID if deposit.paid_amount >= deposit.required_amount else AdmissionDepositStatus.PARTIAL
        deposit.updated_at = datetime.utcnow()
        session.add(deposit)

        result = await session.execute(select(Applicant).where(Applicant.id == transaction.student_id))
        applicant = result.scalar_one_or_none()
        if applicant and applicant.guardian_email:
            try:
                from services.email_service import email_service
                balance = deposit.required_amount - deposit.paid_amount
                balance_clause = f" A balance of GHS {balance:,.2f} remains." if balance > 0 else " This deposit is now fully paid."
                await email_service.send_email(
                    to=[applicant.guardian_email],
                    subject="Admission deposit payment received",
                    html_body=f"<p>Dear {applicant.guardian_name},</p><p>We received a payment of GHS {transaction.amount_paid:,.2f} towards {applicant.first_name}'s admission deposit.{balance_clause}</p>",
                    text_body=f"We received a payment of GHS {transaction.amount_paid:,.2f} towards {applicant.first_name}'s admission deposit.{balance_clause}",
                )
            except Exception as e:
                logger.error(f"Failed to send deposit payment confirmation email: {e}")

        return {"applied": True, "deposit_id": deposit.id, "status": deposit.status.value}

    async def _send_payment_notifications(
        self,
        session: AsyncSession,
        transaction: OnlineTransaction,
        fee_payment: FeePayment,
        fee: Fee
    ):
        """Send SMS/Email notifications after successful payment"""
        
        try:
            # Get parent info
            parent_result = await session.execute(
                select(Parent).where(Parent.id == transaction.parent_id)
            )
            parent = parent_result.scalar_one_or_none()
            
            if not parent:
                logger.warning(f"Parent not found: {transaction.parent_id}")
                return
            
            # Calculate balance
            balance = fee.amount_due - fee.amount_paid - (fee.discount or 0)

            # SMS notification
            message = (
                f"Fee payment of GHS {transaction.amount_paid:.2f} received. "
                f"Remaining balance: GHS {balance:.2f}. "
                f"Receipt: {fee_payment.receipt_number}"
            )
            
            if parent.phone_number:
                try:
                    await sms_service.send_sms(parent.phone_number, message)
                    logger.info(f"SMS sent to {parent.phone_number}")
                except Exception as e:
                    logger.error(f"SMS send error: {str(e)}")
        
        except Exception as e:
            logger.error(f"Error sending notifications: {str(e)}")
    
    async def _distribute_payment_to_fees(
        self,
        session: AsyncSession,
        student_id: str,
        school_id: str,
        amount_to_distribute: float,
        reference_number: str,
        received_by: str = "online_system"
    ) -> tuple[float, list]:
        """
        Distribute a payment to student's outstanding fees
        
        Applies payment to fees in order until exhausted:
        1. Cap payment to each fee's outstanding balance
        2. Pass excess to next fee
        3. Repeat until payment exhausted or all fees covered
        
        Returns:
        (remaining_amount_after_distribution, list_of_fee_payment_records_created)
        """
        
        fee_payments_created = []
        remaining_amount = amount_to_distribute
        
        # Get all outstanding fees for this student, ordered by creation date
        fees_result = await session.execute(
            select(Fee).where(
                and_(
                    Fee.student_id == student_id,
                    Fee.school_id == school_id,
                    Fee.status.in_(["pending", "partial", "overdue"])
                )
            ).order_by(Fee.created_at)
        )
        outstanding_fees = fees_result.scalars().all()
        
        if not outstanding_fees:
            logger.warning(f"No outstanding fees found for student {student_id}")
            return remaining_amount, fee_payments_created
        
        # Distribute payment across fees
        for fee in outstanding_fees:
            if remaining_amount <= 0:
                break
            
            # Calculate outstanding balance for this fee
            fee_balance = fee.amount_due - fee.amount_paid - fee.discount
            
            if fee_balance <= 0:
                # Fee already fully paid, skip to next
                continue
            
            # Determine how much to apply to this fee
            amount_for_this_fee = min(remaining_amount, fee_balance)
            
            # Create FeePayment record
            fee_payment = FeePayment(
                school_id=school_id,
                fee_id=fee.id,
                student_id=student_id,
                amount=amount_for_this_fee,
                payment_method=PaymentMethod.ONLINE_PAYMENT_PAYSTACK.value,
                reference_number=reference_number,
                receipt_number=await get_next_receipt_number(session, school_id),
                payment_date=datetime.utcnow().isoformat(),
                remarks=f"Online payment via Paystack (Ref: {reference_number})",
                received_by=received_by
            )
            session.add(fee_payment)
            fee_payments_created.append(fee_payment)
            
            # Update fee with payment
            fee.amount_paid += amount_for_this_fee
            
            # Update fee status
            fee_balance_after = fee.amount_due - fee.amount_paid - fee.discount
            if fee_balance_after <= 0:
                fee.status = PaymentStatus.PAID.value
            else:
                fee.status = PaymentStatus.PARTIAL.value
            
            fee.updated_at = datetime.utcnow()
            session.add(fee)
            
            # Flush to ensure FeePayment has ID before GL posting
            await session.flush()
            
            # Create GL journal entry for this fee payment
            try:
                # Get fee structure for GL posting
                fee_structure_result = await session.execute(
                    select(FeeStructure).where(FeeStructure.id == fee.fee_structure_id)
                )
                fee_structure = fee_structure_result.scalar_one_or_none()
                
                if fee_structure:
                    await self._create_fee_journal_entry_for_online_payment(
                        session=session,
                        school_id=school_id,
                        payment=fee_payment,
                        fee=fee,
                        fee_structure=fee_structure,
                        amount=amount_for_this_fee,
                    )
                    logger.info(f"Created GL journal entry for online fee payment {fee_payment.id}")
            except Exception as e:
                logger.error(f"Error creating GL journal entry for online fee payment: {str(e)}")
                # Continue processing payments even if GL posting fails
            
            # Reduce remaining amount
            remaining_amount -= amount_for_this_fee
            
            logger.info(
                f"Applied GHS {amount_for_this_fee} to fee {fee.id}, "
                f"balance now: {fee_balance_after:.2f}"
            )
        
        return remaining_amount, fee_payments_created
    
    async def _create_fee_journal_entry_for_online_payment(
        self,
        session: AsyncSession,
        school_id: str,
        payment: FeePayment,
        fee: Fee,
        fee_structure: FeeStructure,
        amount: float,
    ) -> Optional[str]:
        """
        Create a journal entry for an online (Paystack) fee payment posting
        to GL: Dr. 1040 (Paystack Clearing Account) / Cr. 1100 (Accounts
        Receivable) — clearing the receivable that was recognized as
        revenue at INVOICE time (fee_gl_service.post_fee_invoice), not
        crediting revenue again here. Posted to the CLEARING account, not
        1010 (Business Checking) directly — this cash hasn't reached the
        school's real bank yet; it only does once a settlement withdrawal
        completes (services.fee_gl_service.post_settlement_withdrawal).
        """
        try:
            from services import fee_gl_service
            return await fee_gl_service.post_fee_payment(session, school_id, payment, amount, cash_account_code="1040")
        except Exception as e:
            logger.error(f"Error creating GL journal entry for online fee payment: {str(e)}")
            return None
