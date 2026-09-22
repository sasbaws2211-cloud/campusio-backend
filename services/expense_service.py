"""Expense Service - Management of school expenses with approval workflow

Handles expense CRUD, approval workflow, and GL posting with GL account balance updates.
"""
import logging
from typing import Optional, List, Dict, Any, Tuple
from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, and_
from datetime import datetime
from fastapi import BackgroundTasks

from models.finance import (
    Expense,
    ExpenseCategory,
    ExpenseStatus,
    PaymentStatus,
    JournalEntryCreate,
    JournalLineItemCreate,
    ReferenceType,
)
from models.finance.chart_of_accounts import GLAccount
from models.school import School
from services.coa_service import CoaService
from services.exchange_rate_service import ExchangeRateService

logger = logging.getLogger(__name__)


class ExpenseError(Exception):
    """Base exception for expense service errors"""
    pass


class ExpenseValidationError(ExpenseError):
    """Raised when expense validation fails"""
    pass


class ExpenseService:
    """Service for managing school expenses"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def requires_maker_checker(self, school_id: str) -> bool:
        """Whether this school has segregation-of-duties enabled

        Off by default (School.require_maker_checker) — small schools with a
        single finance staffer can't otherwise use the approval workflow.
        """
        result = await self.session.execute(
            select(School.require_maker_checker).where(School.id == school_id)
        )
        return bool(result.scalar_one_or_none())

    async def _enforces_budget_limits(self, school_id: str) -> bool:
        """Whether this school hard-blocks an expense approval that would
        exceed its budgeted amount for that account/period, vs. only
        surfacing it as a warning. Off by default (School.enforce_budget_limits)."""
        result = await self.session.execute(
            select(School.enforce_budget_limits).where(School.id == school_id)
        )
        return bool(result.scalar_one_or_none())

    async def create_expense(
        self,
        school_id: str,
        expense_data,
        created_by: str,
    ) -> Dict[str, Any]:
        """Create a new expense record in DRAFT status
        
        Args:
            school_id: School identifier
            expense_data: ExpenseCreate with expense details
            created_by: User creating the expense
            
        Returns:
            Expense as dictionary
            
        Raises:
            ExpenseValidationError: If validation fails
        """
        try:
            # Validate amount
            if expense_data.amount <= 0:
                raise ExpenseValidationError("Amount must be positive")
            
            # If GL account provided, validate it exists
            if expense_data.gl_account_id:
                result = await self.session.execute(
                    select(GLAccount).where(
                        and_(
                            GLAccount.id == expense_data.gl_account_id,
                            GLAccount.school_id == school_id,
                            GLAccount.is_active == True
                        )
                    )
                )
                gl_account = result.scalar_one_or_none()
                if not gl_account:
                    raise ExpenseValidationError(f"GL account {expense_data.gl_account_id} not found or inactive")
                gl_account_code = gl_account.account_code
            else:
                gl_account_code = expense_data.gl_account_code

            vendor_name = expense_data.vendor_name
            vendor_id = getattr(expense_data, "vendor_id", None)
            if vendor_id:
                from models.finance.expenses import Vendor
                vendor_result = await self.session.execute(
                    select(Vendor).where(Vendor.id == vendor_id, Vendor.school_id == school_id, Vendor.is_active == True)  # noqa: E712
                )
                vendor = vendor_result.scalar_one_or_none()
                if not vendor:
                    raise ExpenseValidationError(f"Vendor {vendor_id} not found or inactive")
                vendor_name = vendor_name or vendor.name

            # Create expense
            expense = Expense(
                school_id=school_id,
                category=expense_data.category,
                description=expense_data.description,
                vendor_name=vendor_name,
                vendor_id=vendor_id,
                amount=expense_data.amount,
                currency=expense_data.currency,
                gl_account_id=expense_data.gl_account_id,
                gl_account_code=gl_account_code,
                expense_date=expense_data.expense_date,
                status=ExpenseStatus.DRAFT,
                created_by=created_by,
                notes=expense_data.notes,
            )
            
            self.session.add(expense)
            await self.session.commit()
            
            logger.info(f"Created expense {expense.id} for school {school_id}")
            return await self._expense_to_dict(expense)
            
        except (ExpenseValidationError, ExpenseError):
            await self.session.rollback()
            raise
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error creating expense: {str(e)}")
            raise ExpenseError(f"Error creating expense: {str(e)}")
    
    async def get_expense_by_id(
        self,
        school_id: str,
        expense_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Get expense by ID
        
        Args:
            school_id: School identifier
            expense_id: Expense to retrieve
            
        Returns:
            Expense dictionary or None if not found
        """
        try:
            result = await self.session.execute(
                select(Expense).where(
                    and_(
                        Expense.id == expense_id,
                        Expense.school_id == school_id
                    )
                )
            )
            expense = result.scalar_one_or_none()
            if not expense:
                return None
            return await self._expense_to_dict(expense)
        except Exception as e:
            logger.error(f"Error retrieving expense {expense_id}: {str(e)}")
            raise ExpenseError(f"Error retrieving expense: {str(e)}")
    
    async def get_expenses_filtered(
        self,
        school_id: str,
        category: Optional[ExpenseCategory] = None,
        status: Optional[ExpenseStatus] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        skip: int = 0,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Get filtered list of expenses with pagination
        
        Args:
            school_id: School identifier
            category: Optional filter by category
            status: Optional filter by status
            start_date: Optional start date filter
            end_date: Optional end date filter
            skip: Pagination offset
            limit: Pagination limit
            
        Returns:
            List of expense dictionaries
        """
        try:
            query = select(Expense).where(
                Expense.school_id == school_id
            )
            
            if category:
                query = query.where(Expense.category == category)
            if status:
                query = query.where(Expense.status == status)
            if start_date:
                query = query.where(Expense.expense_date >= start_date)
            if end_date:
                query = query.where(Expense.expense_date <= end_date)
            
            query = query.order_by(Expense.expense_date.desc())
            query = query.offset(skip).limit(limit)
            
            result = await self.session.execute(query)
            expenses = result.scalars().all()
            
            return [await self._expense_to_dict(exp) for exp in expenses]
        except Exception as e:
            logger.error(f"Error filtering expenses: {str(e)}")
            raise ExpenseError(f"Error filtering expenses: {str(e)}")
    
    async def update_expense(
        self,
        school_id: str,
        expense_id: str,
        update_data,
    ) -> Dict[str, Any]:
        """Update a DRAFT expense
        
        Args:
            school_id: School identifier
            expense_id: Expense to update
            update_data: ExpenseUpdate with new values
            
        Returns:
            Updated expense dictionary
            
        Raises:
            ExpenseError: If not in DRAFT status or not found
        """
        try:
            result = await self.session.execute(
                select(Expense).where(
                    and_(
                        Expense.id == expense_id,
                        Expense.school_id == school_id
                    )
                )
            )
            expense = result.scalar_one_or_none()
            
            if not expense:
                raise ExpenseError(f"Expense {expense_id} not found")
            
            if expense.status != ExpenseStatus.DRAFT:
                raise ExpenseError(
                    f"Cannot update expense in {expense.status} status (only DRAFT can be updated)"
                )
            
            # Update fields
            if update_data.category is not None:
                expense.category = update_data.category
            if update_data.description is not None:
                expense.description = update_data.description
            if update_data.vendor_name is not None:
                expense.vendor_name = update_data.vendor_name
            if update_data.amount is not None:
                if update_data.amount <= 0:
                    raise ExpenseValidationError("Amount must be positive")
                expense.amount = update_data.amount
            if update_data.gl_account_id is not None:
                result = await self.session.execute(
                    select(GLAccount).where(
                        and_(
                            GLAccount.id == update_data.gl_account_id,
                            GLAccount.school_id == school_id,
                            GLAccount.is_active == True
                        )
                    )
                )
                gl_account = result.scalar_one_or_none()
                if not gl_account:
                    raise ExpenseValidationError(f"GL account not found or inactive")
                expense.gl_account_id = update_data.gl_account_id
                expense.gl_account_code = gl_account.account_code
            if update_data.gl_account_code is not None:
                expense.gl_account_code = update_data.gl_account_code
            if update_data.expense_date is not None:
                expense.expense_date = update_data.expense_date
            if update_data.notes is not None:
                expense.notes = update_data.notes
            
            expense.updated_at = datetime.utcnow()
            self.session.add(expense)
            await self.session.commit()
            
            return await self._expense_to_dict(expense)
        except (ExpenseError, ExpenseValidationError):
            await self.session.rollback()
            raise
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error updating expense: {str(e)}")
            raise ExpenseError(f"Error updating expense: {str(e)}")

    async def attach_receipt(
        self,
        school_id: str,
        expense_id: str,
        receipt_url: str,
    ) -> Dict[str, Any]:
        """Attach a receipt/invoice file URL to an expense

        Args:
            school_id: School identifier
            expense_id: Expense to attach the receipt to
            receipt_url: URL of the already-saved receipt file

        Returns:
            Updated expense dictionary

        Raises:
            ExpenseError: If expense not found
        """
        try:
            result = await self.session.execute(
                select(Expense).where(
                    and_(
                        Expense.id == expense_id,
                        Expense.school_id == school_id
                    )
                )
            )
            expense = result.scalar_one_or_none()

            if not expense:
                raise ExpenseError(f"Expense {expense_id} not found")

            expense.receipt_url = receipt_url
            expense.updated_at = datetime.utcnow()
            self.session.add(expense)
            await self.session.commit()

            return await self._expense_to_dict(expense)
        except ExpenseError:
            await self.session.rollback()
            raise
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error attaching receipt: {str(e)}")
            raise ExpenseError(f"Error attaching receipt: {str(e)}")

    async def submit_expense(
        self,
        school_id: str,
        expense_id: str,
        submitted_by: str,
    ) -> Dict[str, Any]:
        """Submit expense for approval (DRAFT → PENDING)
        
        Args:
            school_id: School identifier
            expense_id: Expense to submit
            submitted_by: User submitting
            
        Returns:
            Updated expense dictionary
        """
        try:
            result = await self.session.execute(
                select(Expense).where(
                    and_(
                        Expense.id == expense_id,
                        Expense.school_id == school_id
                    )
                )
            )
            expense = result.scalar_one_or_none()
            
            if not expense:
                raise ExpenseError(f"Expense {expense_id} not found")
            
            if expense.status != ExpenseStatus.DRAFT:
                raise ExpenseError(
                    f"Cannot submit expense in {expense.status} status (only DRAFT can be submitted)"
                )
            
            expense.status = ExpenseStatus.PENDING
            expense.submitted_by = submitted_by
            expense.submitted_at = datetime.utcnow()
            expense.updated_at = datetime.utcnow()
            
            self.session.add(expense)
            await self.session.commit()
            
            logger.info(f"Submitted expense {expense_id} for approval")
            return await self._expense_to_dict(expense)
        except ExpenseError:
            await self.session.rollback()
            raise
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error submitting expense: {str(e)}")
            raise ExpenseError(f"Error submitting expense: {str(e)}")
    
    async def approve_expense(
        self,
        school_id: str,
        expense_id: str,
        approved_by: str,
        approval_notes: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_role: str = "finance",
        background_tasks: Optional[BackgroundTasks] = None,
    ) -> Dict[str, Any]:
        """Approve expense and create GL posting (PENDING → APPROVED)
        
        Note: GL posting is deferred until separate approval call.
        Logs audit trail for compliance.
        
        Args:
            school_id: School identifier
            expense_id: Expense to approve
            approved_by: User approving
            approval_notes: Optional approval notes
            ip_address: IP address of approver (for audit trail)
            user_role: Role of approver (for audit trail)
            
        Returns:
            Updated expense dictionary
        """
        try:
            result = await self.session.execute(
                select(Expense).where(
                    and_(
                        Expense.id == expense_id,
                        Expense.school_id == school_id
                    )
                )
            )
            expense = result.scalar_one_or_none()
            
            if not expense:
                raise ExpenseError(f"Expense {expense_id} not found")
            
            if expense.status != ExpenseStatus.PENDING:
                raise ExpenseError(
                    f"Cannot approve expense in {expense.status} status (only PENDING can be approved)"
                )

            if await self.requires_maker_checker(school_id) and expense.submitted_by == approved_by:
                raise ExpenseError(
                    "Segregation of duties: you submitted this expense and cannot also approve it"
                )

            # Budget check — previously "budget" was purely an after-the-fact
            # report (BudgetService.get_budget_vs_actual); nothing in this
            # approval path ever consulted it, so a school could blow
            # through an approved budget with zero warning anywhere. Only
            # HARD-blocks when the school has opted into
            # School.enforce_budget_limits; otherwise this is surfaced as a
            # warning on the response, never silently ignored.
            budget_warning = None
            if expense.gl_account_id:
                from services.budget_service import BudgetService
                budget_check = await BudgetService(self.session).check_budget_available(
                    school_id, expense.gl_account_id, expense.expense_date, expense.amount,
                )
                if budget_check and budget_check["exceeds_budget"]:
                    if await self._enforces_budget_limits(school_id):
                        raise ExpenseError(
                            f"Approving this expense would exceed the budgeted amount for account "
                            f"{budget_check['account_code']} by {budget_check['exceeds_by']:.2f} "
                            f"(budgeted {budget_check['budgeted_amount']:.2f}, would reach {budget_check['projected_after']:.2f})"
                        )
                    budget_warning = budget_check

            expense.status = ExpenseStatus.APPROVED
            expense.approved_by = approved_by
            expense.approved_date = datetime.utcnow()
            expense.updated_at = datetime.utcnow()
            if approval_notes:
                expense.notes = f"{expense.notes or ''}\n[APPROVAL] {approval_notes}".strip()
            
            self.session.add(expense)
            await self.session.commit()

            logger.info(f"Approved expense {expense_id} (amount: {expense.amount})")

            if background_tasks is not None:
                from services.webhook_service import emit_event
                await emit_event(
                    self.session, background_tasks, school_id, "expense.approved",
                    {
                        "id": expense.id,
                        "category": expense.category,
                        "amount": float(expense.amount),
                        "approved_by": approved_by,
                    },
                )

            result_dict = await self._expense_to_dict(expense)
            if budget_warning:
                result_dict["budget_warning"] = {
                    k: (float(v) if isinstance(v, Decimal) else v) for k, v in budget_warning.items()
                }
            return result_dict
        except ExpenseError:
            await self.session.rollback()
            raise
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error approving expense: {str(e)}")
            raise ExpenseError(f"Error approving expense: {str(e)}")

    async def reject_expense(
        self,
        school_id: str,
        expense_id: str,
        rejected_by: str,
        rejection_reason: str,
    ) -> Dict[str, Any]:
        """Reject expense (PENDING → REJECTED)
        
        Args:
            school_id: School identifier
            expense_id: Expense to reject
            rejected_by: User rejecting
            rejection_reason: Reason for rejection
            
        Returns:
            Updated expense dictionary
        """
        try:
            result = await self.session.execute(
                select(Expense).where(
                    and_(
                        Expense.id == expense_id,
                        Expense.school_id == school_id
                    )
                )
            )
            expense = result.scalar_one_or_none()
            
            if not expense:
                raise ExpenseError(f"Expense {expense_id} not found")
            
            if expense.status != ExpenseStatus.PENDING:
                raise ExpenseError(
                    f"Cannot reject expense in {expense.status} status (only PENDING can be rejected)"
                )
            
            expense.status = ExpenseStatus.REJECTED
            expense.rejected_by = rejected_by
            expense.rejected_reason = rejection_reason
            expense.updated_at = datetime.utcnow()
            
            self.session.add(expense)
            await self.session.commit()
            
            logger.info(f"Rejected expense {expense_id}: {rejection_reason}")
            return await self._expense_to_dict(expense)
        except ExpenseError:
            await self.session.rollback()
            raise
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error rejecting expense: {str(e)}")
            raise ExpenseError(f"Error rejecting expense: {str(e)}")
    
    async def post_expense_to_gl(
        self,
        school_id: str,
        expense_id: str,
        posted_by: str,
        ip_address: Optional[str] = None,
        user_role: str = "finance",
    ) -> Dict[str, Any]:
        """Post approved expense to GL (APPROVED → POSTED)
        
        **CRITICAL OPERATION** - Creates a journal entry and posts it to GL,
        updating GL account balances. Logs audit trail for compliance.
        
        Args:
            school_id: School identifier
            expense_id: Expense to post
            posted_by: User posting
            ip_address: IP address of user (for audit trail)
            user_role: Role of user posting (for audit trail)
            
        Returns:
            Updated expense dictionary with journal_entry_id
        """
        try:
            result = await self.session.execute(
                select(Expense).where(
                    and_(
                        Expense.id == expense_id,
                        Expense.school_id == school_id
                    )
                )
            )
            expense = result.scalar_one_or_none()
            
            if not expense:
                raise ExpenseError(f"Expense {expense_id} not found")
            
            if expense.status != ExpenseStatus.APPROVED:
                raise ExpenseError(
                    f"Cannot post expense in {expense.status} status (only APPROVED can be posted)"
                )
            
            # ⭐ CRITICAL: Create GL journal entry (updates GL balances)
            journal_entry_id, base_currency_amount, exchange_rate_applied = await self._create_expense_journal_entry(
                school_id=school_id,
                expense=expense,
                posted_by=posted_by,
                ip_address=ip_address,
                user_role=user_role,
            )

            # Update expense — all together, only once posting actually
            # succeeded (see _create_expense_journal_entry's docstring note).
            expense.status = ExpenseStatus.POSTED
            expense.posted_date = datetime.utcnow()
            expense.posted_by = posted_by
            expense.posted_ip = ip_address
            expense.journal_entry_id = journal_entry_id
            expense.gl_posting_reference = f"JE-{journal_entry_id[:8]}"
            expense.base_currency_amount = base_currency_amount
            expense.exchange_rate_applied = exchange_rate_applied
            expense.updated_at = datetime.utcnow()
            
            self.session.add(expense)
            await self.session.commit()
            
            logger.info(
                f"Posted expense {expense_id} to GL (journal entry {journal_entry_id}), "
                f"amount: {expense.amount}, account: {expense.gl_account_code}"
            )
            return await self._expense_to_dict(expense)
        except ExpenseError:
            await self.session.rollback()
            raise
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error posting expense to GL: {str(e)}")
            raise ExpenseError(f"Error posting expense to GL: {str(e)}")
    
    async def record_payment(
        self,
        school_id: str,
        expense_id: str,
        amount_paid: Decimal,
        paid_by: str,
        payment_date: datetime,
    ) -> Dict[str, Any]:
        """Record payment for an expense
        
        Args:
            school_id: School identifier
            expense_id: Expense to pay
            amount_paid: Amount being paid
            paid_by: User recording payment
            payment_date: Date of payment
            
        Returns:
            Updated expense dictionary
        """
        try:
            result = await self.session.execute(
                select(Expense).where(
                    and_(
                        Expense.id == expense_id,
                        Expense.school_id == school_id
                    )
                )
            )
            expense = result.scalar_one_or_none()
            
            if not expense:
                raise ExpenseError(f"Expense {expense_id} not found")
            
            if amount_paid <= 0:
                raise ExpenseValidationError("Payment amount must be positive")
            
            total_paid = expense.amount_paid + amount_paid
            if total_paid > expense.amount:
                raise ExpenseValidationError(
                    f"Payment exceeds expense amount (expense: {expense.amount}, total paid: {total_paid})"
                )
            
            expense.amount_paid = total_paid
            expense.paid_by = paid_by
            expense.payment_date = payment_date

            # Update payment status
            remaining = expense.amount - total_paid
            if remaining < Decimal("0.01"):  # Account for rounding
                expense.payment_status = PaymentStatus.PAID.value
            elif total_paid > Decimal("0.01"):
                expense.payment_status = PaymentStatus.PARTIAL.value
            else:
                expense.payment_status = PaymentStatus.OUTSTANDING.value

            expense.updated_at = datetime.utcnow()
            self.session.add(expense)

            # Clear the payable as cash actually goes out (Dr Accounts
            # Payable / Cr Bank) — only once the expense itself has
            # actually been posted to GL (an unposted DRAFT/PENDING/
            # APPROVED expense has no payable on the books yet to clear).
            # Previously this function never touched the GL at all — its
            # own router endpoint's docstring said outright "Does not
            # affect GL posting."
            payment_journal_entry_id = None
            if expense.status == ExpenseStatus.POSTED:
                try:
                    payment_journal_entry_id = await self._create_expense_payment_journal_entry(
                        school_id=school_id, expense=expense, amount_paid=amount_paid, paid_by=paid_by, payment_date=payment_date,
                    )
                except Exception as e:
                    logger.error(f"Error posting expense payment journal entry for {expense_id}: {str(e)}")

            await self.session.commit()

            logger.info(f"Recorded {amount_paid} payment for expense {expense_id}")
            result_dict = await self._expense_to_dict(expense)
            if payment_journal_entry_id:
                result_dict["payment_journal_entry_id"] = payment_journal_entry_id
            return result_dict
        except (ExpenseError, ExpenseValidationError):
            await self.session.rollback()
            raise
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error recording payment: {str(e)}")
            raise ExpenseError(f"Error recording payment: {str(e)}")
    
    async def get_expense_summary(
        self,
        school_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Get expense summary statistics
        
        Args:
            school_id: School identifier
            start_date: Optional start date filter
            end_date: Optional end date filter
            
        Returns:
            Summary dictionary with counts and totals
        """
        try:
            query = select(Expense).where(
                Expense.school_id == school_id
            )
            
            if start_date:
                query = query.where(Expense.expense_date >= start_date)
            if end_date:
                query = query.where(Expense.expense_date <= end_date)
            
            result = await self.session.execute(query)
            expenses = result.scalars().all()
            
            summary = {
                "total_expenses": len(expenses),
                "draft_count": 0,
                "pending_count": 0,
                "approved_count": 0,
                "posted_count": 0,
                "rejected_count": 0,
                "total_amount": Decimal("0"),
                "total_paid": Decimal("0"),
                "outstanding_amount": Decimal("0"),
                "by_category": {}
            }
            
            for expense in expenses:
                # Count by status
                if expense.status == ExpenseStatus.DRAFT:
                    summary["draft_count"] += 1
                elif expense.status == ExpenseStatus.PENDING:
                    summary["pending_count"] += 1
                elif expense.status == ExpenseStatus.APPROVED:
                    summary["approved_count"] += 1
                elif expense.status == ExpenseStatus.POSTED:
                    summary["posted_count"] += 1
                elif expense.status == ExpenseStatus.REJECTED:
                    summary["rejected_count"] += 1
                
                # Totals
                summary["total_amount"] += expense.amount
                summary["total_paid"] += expense.amount_paid
                summary["outstanding_amount"] += (expense.amount - expense.amount_paid)
                
                # By category
                cat = expense.category.value
                if cat not in summary["by_category"]:
                    summary["by_category"][cat] = {
                        "count": 0,
                        "total_amount": Decimal("0"),
                        "total_paid": Decimal("0"),
                    }
                summary["by_category"][cat]["count"] += 1
                summary["by_category"][cat]["total_amount"] += expense.amount
                summary["by_category"][cat]["total_paid"] += expense.amount_paid
            
            return summary
        except Exception as e:
            logger.error(f"Error generating expense summary: {str(e)}")
            raise ExpenseError(f"Error generating expense summary: {str(e)}")
    
    async def _create_expense_journal_entry(
        self,
        school_id: str,
        expense: Expense,
        posted_by: str,
        ip_address: Optional[str] = None,
        user_role: str = "finance",
    ) -> Tuple[str, Decimal, Decimal]:
        """Create and post GL journal entry for expense

        **CRITICAL OPERATION** - Posts:
        - Dr. Expense GL account: amount
        - Cr. Accounts Payable (2200): amount

        Previously this credited the Bank account directly, meaning EVERY
        expense posted as if it was paid in cash the same day regardless of
        payment_status — there was no way to record "we owe this, due in
        30 days" at all, and no vendor-owed (accounts payable) balance ever
        existed on the books. Now posts a real payable; record_payment
        clears it (Dr Accounts Payable / Cr Bank) as cash actually goes out.

        Updates GL account balances via journal entry posting.

        Returns:
            (journal_entry_id, base_currency_amount, exchange_rate_applied) —
            the caller is responsible for writing these onto the Expense
            once it's actually finished POSTED, not before (see the note in
            the method body on why this doesn't mutate `expense` itself).

        Args:
            school_id: School identifier
            expense: Expense to post
            posted_by: User posting
            ip_address: IP address for audit trail
            user_role: Role for audit trail
            
        Returns:
            Journal entry ID
            
        Raises:
            Exception: If GL accounts not found
        """
        from services.journal_entry_service import JournalEntryService
        
        # Get expense GL account
        if not expense.gl_account_id:
            raise ExpenseError(f"Expense has no GL account assigned")
        
        result = await self.session.execute(
            select(GLAccount).where(
                and_(
                    GLAccount.id == expense.gl_account_id,
                    GLAccount.school_id == school_id,
                    GLAccount.is_active == True
                )
            )
        )
        expense_account = result.scalar_one_or_none()
        
        if not expense_account:
            raise ExpenseError(f"GL account {expense.gl_account_id} not found or inactive")
        
        # The accounts-payable liability this expense is posted against —
        # looked up by system_role so a school can rename/replace 2200
        # without breaking expense posting (falls back to "2200" for
        # schools seeded before system_role existed on this account).
        payable_account = await CoaService(self.session).get_system_account(
            school_id=school_id,
            system_role="accounts_payable_vendors",
            fallback_code="2200",
        )

        if not payable_account:
            raise ExpenseError(
                "No accounts payable account configured (expected a GL account with "
                "system_role='accounts_payable_vendors' or account code 2200)"
            )

        # GL accounts have no currency of their own — they're implicitly in
        # the school's base currency. An expense recorded in any other
        # currency must be converted before posting, using the rate
        # applicable on expense_date, or every non-base-currency expense
        # would silently post its foreign-currency face value straight into
        # base-currency accounts.
        base_currency = await ExchangeRateService(self.session).get_school_base_currency(school_id)
        if expense.currency.upper() != base_currency.upper():
            rate = await ExchangeRateService(self.session).get_rate(
                school_id=school_id,
                from_currency=expense.currency,
                to_currency=base_currency,
                as_of_date=expense.expense_date,
            )
            if rate is None:
                raise ExpenseError(
                    f"No exchange rate found for {expense.currency} -> {base_currency} "
                    f"on or before {expense.expense_date.date()}. Record one before posting."
                )
            posting_amount = (expense.amount * rate).quantize(Decimal("0.01"))
            base_currency_amount = posting_amount
            exchange_rate_applied = rate
        else:
            posting_amount = expense.amount
            base_currency_amount = expense.amount
            exchange_rate_applied = Decimal("1")

        # Build journal entry. posting_amount is already Decimal — no float()
        # round-trip here, which would otherwise reintroduce binary-float
        # rounding right before the two sides get compared for balance.
        journal_line_items = [
            # Debit: Expense account
            JournalLineItemCreate(
                gl_account_id=expense_account.id,
                debit_amount=posting_amount,
                credit_amount=Decimal("0"),
                description=f"{expense.category.value}: {expense.description}",
            ),
            # Credit: Accounts Payable (a bill owed, not cash already gone)
            JournalLineItemCreate(
                gl_account_id=payable_account.id,
                debit_amount=Decimal("0"),
                credit_amount=posting_amount,
                description=f"Payable to {expense.vendor_name or 'vendor'} - {expense.description}",
            ),
        ]

        base_note = f"Expense from {expense.vendor_name or 'vendor'}" if expense.vendor_name else "Expense posting"
        if expense.currency.upper() != base_currency.upper():
            base_note += (
                f" ({expense.amount} {expense.currency.upper()} @ {exchange_rate_applied} "
                f"= {posting_amount} {base_currency.upper()})"
            )

        entry_data = JournalEntryCreate(
            entry_date=expense.expense_date,
            reference_type=ReferenceType.EXPENSE,
            reference_id=expense.id,
            description=f"Expense: {expense.description}",
            line_items=journal_line_items,
            notes=base_note,
        )

        # Create and post entry (⭐ this updates GL balances). Deliberately
        # not mutating `expense` at all until this succeeds — create_entry()
        # commits internally, and since it shares this same session, that
        # commit would otherwise persist any earlier attribute changes made
        # on `expense` even if post_entry() then fails (e.g. the period
        # being locked), leaving a half-posted-looking expense with
        # conversion numbers set but no journal_entry_id or POSTED status.
        journal_service = JournalEntryService(self.session)
        entry = await journal_service.create_entry(
            school_id=school_id,
            entry_data=entry_data,
            created_by="SYSTEM",
        )

        posted_entry = await journal_service.post_entry(
            school_id=school_id,
            entry_id=entry.id,
            posted_by=posted_by,
            approval_notes=f"Expense {expense.id} from {expense.vendor_name or 'vendor'}: auto-posted",
            ip_address=ip_address,
            user_role=user_role,
        )

        return posted_entry.id, base_currency_amount, exchange_rate_applied

    async def _create_expense_payment_journal_entry(
        self,
        school_id: str,
        expense: Expense,
        amount_paid: Decimal,
        paid_by: str,
        payment_date: datetime,
    ) -> Optional[str]:
        """Dr Accounts Payable (2200) / Cr Bank — the payable this expense
        posted at post_expense_to_gl time being cleared as cash actually
        goes out. Posted in the expense's OWN currency conversion (using
        base_currency_amount's implied rate) so a foreign-currency
        expense's payment clears the exact payable amount that was posted,
        not a fresh conversion at today's rate."""
        from services.journal_entry_service import JournalEntryService

        payable_account = await CoaService(self.session).get_system_account(
            school_id=school_id, system_role="accounts_payable_vendors", fallback_code="2200",
        )
        if not payable_account:
            raise ExpenseError("No accounts payable account configured")

        bank_account = await CoaService(self.session).get_system_account(
            school_id=school_id, system_role="default_cash_account", fallback_code="1010",
        )
        if not bank_account:
            raise ExpenseError("No default cash account configured")

        # Convert the payment using the SAME rate the original posting
        # used, not a fresh lookup — a payable of 1000 EUR posted at 12.5
        # (=12,500 GHS) must clear exactly 12,500 GHS when fully paid,
        # regardless of what today's EUR rate happens to be.
        if expense.exchange_rate_applied and expense.exchange_rate_applied != Decimal("1"):
            posting_amount = (amount_paid * expense.exchange_rate_applied).quantize(Decimal("0.01"))
        else:
            posting_amount = amount_paid

        entry_data = JournalEntryCreate(
            entry_date=payment_date,
            reference_type=ReferenceType.EXPENSE,
            reference_id=expense.id,
            description=f"Payment to {expense.vendor_name or 'vendor'} for {expense.description}",
            line_items=[
                JournalLineItemCreate(gl_account_id=payable_account.id, debit_amount=posting_amount, credit_amount=Decimal("0"), description=f"Payable cleared - {expense.description}"),
                JournalLineItemCreate(gl_account_id=bank_account.id, debit_amount=Decimal("0"), credit_amount=posting_amount, description=f"Payment to {expense.vendor_name or 'vendor'}"),
            ],
            notes=f"Auto-posted expense payment for {expense.id}",
        )
        journal_service = JournalEntryService(self.session)
        entry = await journal_service.create_entry(school_id=school_id, entry_data=entry_data, created_by=paid_by)
        posted_entry = await journal_service.post_entry(
            school_id=school_id, entry_id=entry.id, posted_by=paid_by, approval_notes="Auto-posted expense payment",
        )
        return posted_entry.id

    async def _expense_to_dict(self, expense: Expense) -> Dict[str, Any]:
        """Convert Expense object to dictionary"""
        return {
            "id": expense.id,
            "school_id": expense.school_id,
            "category": expense.category,
            "description": expense.description,
            "vendor_name": expense.vendor_name,
            "amount": expense.amount,
            "currency": expense.currency,
            "base_currency_amount": expense.base_currency_amount,
            "exchange_rate_applied": expense.exchange_rate_applied,
            "gl_account_id": expense.gl_account_id,
            "gl_account_code": expense.gl_account_code,
            "expense_date": expense.expense_date,
            "status": expense.status,
            "payment_status": expense.payment_status,
            "amount_paid": expense.amount_paid,
            "submitted_by": expense.submitted_by,
            "submitted_at": expense.submitted_at,
            "approved_by": expense.approved_by,
            "approved_date": expense.approved_date,
            "rejected_reason": expense.rejected_reason,
            "journal_entry_id": expense.journal_entry_id,
            "receipt_url": expense.receipt_url,
            "notes": expense.notes,
            "created_by": expense.created_by,
            "created_at": expense.created_at,
            "updated_at": expense.updated_at,
        }
