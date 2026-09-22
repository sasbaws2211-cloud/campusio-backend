"""Staff Loan Service - loan/advance requests, approval, and payroll repayment"""
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from models.staff_loan import StaffLoan, StaffLoanCreate, StaffLoanRepayment, LoanStatus
from models.payroll import PayrollAdjustment

logger = logging.getLogger(__name__)

# GL account 1210 "Staff Loans Receivable" — see models/finance/seed_coa.py.
# 1200 was already taken (Prepaid Expenses), so this loan-receivable account
# uses the next free code in the 1200s asset range.
STAFF_LOAN_RECEIVABLE_ACCOUNT_CODE = "1210"


class StaffLoanService:
    """Service for staff loan/advance requests and their payroll repayments."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_loan(
        self,
        school_id: str,
        data: StaffLoanCreate,
        requested_by: str,
    ) -> Dict[str, Any]:
        """Create a pending loan/advance request."""
        try:
            loan = StaffLoan(
                school_id=school_id,
                staff_id=data.staff_id,
                loan_type=data.loan_type,
                principal_amount=data.principal_amount,
                installment_amount=data.installment_amount,
                total_installments=data.total_installments,
                reason=data.reason,
                start_period_year=data.start_period_year,
                start_period_month=data.start_period_month,
                notes=data.notes,
                requested_by=requested_by,
                status=LoanStatus.PENDING,
            )
            self.session.add(loan)
            await self.session.commit()
            await self.session.refresh(loan)
            return {"success": True, "loan_id": loan.id, "message": "Loan request created, pending approval"}
        except Exception as e:
            logger.error(f"Error creating staff loan: {str(e)}")
            await self.session.rollback()
            return {"success": False, "error": str(e)}

    async def get_loan(self, school_id: str, loan_id: str) -> Optional[StaffLoan]:
        result = await self.session.execute(
            select(StaffLoan).where(StaffLoan.id == loan_id, StaffLoan.school_id == school_id)
        )
        return result.scalar_one_or_none()

    async def list_loans(
        self,
        school_id: str,
        staff_id: Optional[str] = None,
        status: Optional[LoanStatus] = None,
    ) -> List[StaffLoan]:
        query = select(StaffLoan).where(StaffLoan.school_id == school_id)
        if staff_id:
            query = query.where(StaffLoan.staff_id == staff_id)
        if status:
            query = query.where(StaffLoan.status == status)
        query = query.order_by(StaffLoan.created_at.desc())
        result = await self.session.execute(query)
        return result.scalars().all()

    async def approve_loan(
        self,
        school_id: str,
        loan_id: str,
        approved_by: str,
    ) -> Dict[str, Any]:
        """Approve a pending loan: activates it, sets the outstanding
        balance to the full principal, and posts a disbursement journal
        entry (Dr Staff Loans Receivable / Cr the school's default cash
        account) so the receivable shows up on the balance sheet from the
        moment the money actually leaves the school's account."""
        from services.journal_entry_service import JournalEntryService
        from services.coa_service import CoaService
        from models.finance import JournalEntryCreate, JournalLineItemCreate, ReferenceType

        try:
            loan = await self.get_loan(school_id, loan_id)
            if not loan:
                return {"success": False, "error": "Loan not found"}
            if loan.status != LoanStatus.PENDING:
                return {"success": False, "error": f"Cannot approve a loan in {loan.status} status"}
            if loan.requested_by == approved_by:
                return {"success": False, "error": "You cannot approve a loan you requested yourself — ask another admin to approve it"}

            coa_service = CoaService(self.session)
            receivable_account = await coa_service.get_system_account(
                school_id, "staff_loan_receivable", fallback_code=STAFF_LOAN_RECEIVABLE_ACCOUNT_CODE
            )
            cash_account = await coa_service.get_system_account(
                school_id, "default_cash_account", fallback_code="1010"
            )

            journal_entry_id = None
            if receivable_account and cash_account:
                try:
                    entry_data = JournalEntryCreate(
                        entry_date=datetime.utcnow(),
                        reference_type=ReferenceType.ADJUSTMENT,
                        reference_id=loan.id,
                        description=f"Staff {loan.loan_type} disbursement — {loan.staff_id}",
                        line_items=[
                            JournalLineItemCreate(
                                gl_account_id=receivable_account.id,
                                debit_amount=loan.principal_amount,
                                credit_amount=0.0,
                                description=f"Staff {loan.loan_type} receivable",
                            ),
                            JournalLineItemCreate(
                                gl_account_id=cash_account.id,
                                debit_amount=0.0,
                                credit_amount=loan.principal_amount,
                                description=f"Disbursement of staff {loan.loan_type}",
                            ),
                        ],
                        notes=f"Auto-posted on approval of staff loan {loan.id}",
                    )
                    journal_service = JournalEntryService(self.session)
                    entry = await journal_service.create_entry(
                        school_id=school_id, entry_data=entry_data, created_by="SYSTEM"
                    )
                    posted = await journal_service.post_entry(
                        school_id=school_id, entry_id=entry.id, posted_by="SYSTEM",
                        approval_notes="Auto-posted on loan approval",
                    )
                    journal_entry_id = posted.id
                except Exception as e:
                    # Same trade-off as payroll posting: don't block the
                    # approval itself on a GL failure, log for reconciliation.
                    logger.error(f"Error posting loan disbursement journal entry: {str(e)}")
            else:
                logger.error(
                    f"Cannot post loan disbursement for school {school_id}: "
                    f"missing GL account(s) (receivable={bool(receivable_account)}, cash={bool(cash_account)})"
                )

            loan.status = LoanStatus.ACTIVE
            loan.outstanding_balance = loan.principal_amount
            loan.approved_by = approved_by
            loan.approved_at = datetime.utcnow()
            loan.disbursed_at = datetime.utcnow()
            loan.updated_at = datetime.utcnow()
            self.session.add(loan)
            await self.session.commit()

            response = {"success": True, "loan_id": loan.id, "message": "Loan approved and disbursed"}
            if journal_entry_id:
                response["journal_entry_id"] = journal_entry_id
            return response
        except Exception as e:
            logger.error(f"Error approving staff loan: {str(e)}")
            await self.session.rollback()
            return {"success": False, "error": str(e)}

    async def reject_loan(self, school_id: str, loan_id: str, rejected_by: str) -> Dict[str, Any]:
        try:
            loan = await self.get_loan(school_id, loan_id)
            if not loan:
                return {"success": False, "error": "Loan not found"}
            if loan.status != LoanStatus.PENDING:
                return {"success": False, "error": f"Cannot reject a loan in {loan.status} status"}

            loan.status = LoanStatus.REJECTED
            loan.approved_by = rejected_by
            loan.approved_at = datetime.utcnow()
            loan.updated_at = datetime.utcnow()
            self.session.add(loan)
            await self.session.commit()
            return {"success": True, "message": "Loan rejected"}
        except Exception as e:
            logger.error(f"Error rejecting staff loan: {str(e)}")
            await self.session.rollback()
            return {"success": False, "error": str(e)}

    async def write_off_loan(
        self,
        school_id: str,
        loan_id: str,
        written_off_by: str,
        reason: str,
    ) -> Dict[str, Any]:
        """Write off a loan's remaining outstanding balance — e.g. a
        departing staff member whose final settlement won't recover it.
        LoanStatus.CANCELLED existed on the model but had no code path
        ever setting it — there was no way to close out a loan except
        letting payroll deductions run to completion, which doesn't help
        when the staff member has already left. Posts a reversing GL entry
        (Dr Bad Debt Expense / Cr Staff Loans Receivable) for the
        outstanding amount, matching approve_loan's own disbursement
        posting pattern, then marks the loan CANCELLED so it drops out of
        get_due_loan_deductions and future payroll runs stop trying to
        collect it."""
        from services.journal_entry_service import JournalEntryService
        from services.coa_service import CoaService
        from models.finance import JournalEntryCreate, JournalLineItemCreate, ReferenceType

        try:
            loan = await self.get_loan(school_id, loan_id)
            if not loan:
                return {"success": False, "error": "Loan not found"}
            if loan.status != LoanStatus.ACTIVE:
                return {"success": False, "error": f"Cannot write off a loan in {loan.status} status — only an active, disbursed loan can be written off"}
            if loan.outstanding_balance <= 0:
                return {"success": False, "error": "Loan has no outstanding balance to write off"}
            if loan.requested_by == written_off_by:
                return {"success": False, "error": "You cannot write off a loan you requested yourself — ask another admin to write it off"}

            outstanding = loan.outstanding_balance

            coa_service = CoaService(self.session)
            receivable_account = await coa_service.get_system_account(
                school_id, "staff_loan_receivable", fallback_code=STAFF_LOAN_RECEIVABLE_ACCOUNT_CODE
            )
            bad_debt_account = await coa_service.get_system_account(
                school_id, "bad_debt_expense", fallback_code="5900"
            )

            journal_entry_id = None
            if receivable_account and bad_debt_account:
                try:
                    entry_data = JournalEntryCreate(
                        entry_date=datetime.utcnow(),
                        reference_type=ReferenceType.ADJUSTMENT,
                        reference_id=loan.id,
                        description=f"Write-off of staff {loan.loan_type} — {loan.staff_id}: {reason}",
                        line_items=[
                            JournalLineItemCreate(
                                gl_account_id=bad_debt_account.id,
                                debit_amount=outstanding,
                                credit_amount=0.0,
                                description=f"Write-off of staff {loan.loan_type}",
                            ),
                            JournalLineItemCreate(
                                gl_account_id=receivable_account.id,
                                debit_amount=0.0,
                                credit_amount=outstanding,
                                description=f"Staff {loan.loan_type} receivable written off",
                            ),
                        ],
                        notes=f"Auto-posted on write-off of staff loan {loan.id}",
                    )
                    journal_service = JournalEntryService(self.session)
                    entry = await journal_service.create_entry(
                        school_id=school_id, entry_data=entry_data, created_by="SYSTEM"
                    )
                    posted = await journal_service.post_entry(
                        school_id=school_id, entry_id=entry.id, posted_by="SYSTEM",
                        approval_notes="Auto-posted on loan write-off",
                    )
                    journal_entry_id = posted.id
                except Exception as e:
                    # Same trade-off as approve_loan's own GL posting: don't
                    # block the write-off itself on a GL failure, log it.
                    logger.error(f"Error posting loan write-off journal entry: {str(e)}")
            else:
                logger.error(
                    f"Cannot post loan write-off for school {school_id}: "
                    f"missing GL account(s) (receivable={bool(receivable_account)}, bad_debt={bool(bad_debt_account)})"
                )

            loan.status = LoanStatus.CANCELLED
            loan.outstanding_balance = 0.0
            loan.approved_by = written_off_by
            loan.approved_at = datetime.utcnow()
            loan.notes = f"{loan.notes + ' | ' if loan.notes else ''}Written off: {reason}"
            loan.updated_at = datetime.utcnow()
            self.session.add(loan)
            await self.session.commit()

            response = {"success": True, "loan_id": loan.id, "written_off_amount": outstanding, "message": "Loan written off"}
            if journal_entry_id:
                response["journal_entry_id"] = journal_entry_id
            return response
        except Exception as e:
            logger.error(f"Error writing off staff loan: {str(e)}")
            await self.session.rollback()
            return {"success": False, "error": str(e)}

    async def settle_loan_at_exit(
        self,
        school_id: str,
        loan_id: str,
        staff_exit_id: str,
        settled_by: str,
    ) -> Dict[str, Any]:
        """Pay off a loan's full outstanding balance in one shot at staff
        exit, instead of the normal per-payroll-run installment path —
        previously the only options at exit were letting installments run
        (too slow; the staff member is leaving) or write_off_loan
        (forgives a perfectly recoverable debt). Recovers the balance
        against the school's own Salaries Payable account, standing in for
        "amount owed to the departing staff member's final settlement" —
        same accounts a normal installment deduction eventually credits
        via _create_payroll_journal_entry, just posted directly since
        there's no payroll run context to piggyback on here."""
        from services.journal_entry_service import JournalEntryService
        from services.gl_account_helpers import get_or_create_system_account
        from models.finance import JournalEntryCreate, JournalLineItemCreate, ReferenceType

        try:
            loan = await self.get_loan(school_id, loan_id)
            if not loan:
                return {"success": False, "error": "Loan not found"}
            if loan.status != LoanStatus.ACTIVE:
                return {"success": False, "error": f"Cannot settle a loan in {loan.status} status — only an active, disbursed loan can be settled"}
            if loan.outstanding_balance <= 0:
                return {"success": False, "error": "Loan has no outstanding balance to settle"}

            outstanding = loan.outstanding_balance

            salaries_payable_account = await get_or_create_system_account(self.session, school_id, "2100")
            receivable_account = await get_or_create_system_account(self.session, school_id, STAFF_LOAN_RECEIVABLE_ACCOUNT_CODE)

            entry_data = JournalEntryCreate(
                entry_date=datetime.utcnow(),
                reference_type=ReferenceType.ADJUSTMENT,
                reference_id=loan.id,
                description=f"Staff {loan.loan_type} settled in full at exit — {loan.staff_id}",
                line_items=[
                    JournalLineItemCreate(
                        gl_account_id=salaries_payable_account.id, debit_amount=outstanding, credit_amount=0.0,
                        description=f"Final settlement reduced by outstanding {loan.loan_type}",
                    ),
                    JournalLineItemCreate(
                        gl_account_id=receivable_account.id, debit_amount=0.0, credit_amount=outstanding,
                        description=f"Staff {loan.loan_type} receivable settled",
                    ),
                ],
                notes=f"Auto-posted exit settlement for staff loan {loan.id}",
            )
            journal_service = JournalEntryService(self.session)
            entry = await journal_service.create_entry(school_id=school_id, entry_data=entry_data, created_by="SYSTEM")
            posted = await journal_service.post_entry(
                school_id=school_id, entry_id=entry.id, posted_by="SYSTEM",
                approval_notes="Auto-posted loan settlement at staff exit",
            )

            repayment = StaffLoanRepayment(
                school_id=school_id, loan_id=loan.id, staff_id=loan.staff_id,
                staff_exit_id=staff_exit_id,
                installment_number=loan.installments_paid + 1,
                amount_paid=outstanding, balance_after=0.0,
            )
            self.session.add(repayment)

            loan.outstanding_balance = 0.0
            loan.installments_paid = loan.total_installments
            loan.status = LoanStatus.COMPLETED
            loan.updated_at = datetime.utcnow()
            self.session.add(loan)
            await self.session.commit()

            return {
                "success": True, "loan_id": loan.id, "settled_amount": outstanding,
                "journal_entry_id": posted.id, "message": "Loan settled in full from final pay",
            }
        except Exception as e:
            logger.error(f"Error settling staff loan at exit: {str(e)}")
            await self.session.rollback()
            return {"success": False, "error": str(e)}

    async def get_due_loan_deductions(
        self,
        school_id: str,
        staff_id: str,
        period_year: int,
        period_month: int,
    ) -> List[StaffLoan]:
        """Active loans for this staff member whose repayment schedule has
        started by this period and still have a balance outstanding."""
        result = await self.session.execute(
            select(StaffLoan).where(
                StaffLoan.school_id == school_id,
                StaffLoan.staff_id == staff_id,
                StaffLoan.status == LoanStatus.ACTIVE,
                StaffLoan.outstanding_balance > 0.01,
            )
        )
        loans = result.scalars().all()
        due = [
            loan for loan in loans
            if (loan.start_period_year, loan.start_period_month) <= (period_year, period_month)
        ]
        return due

    async def apply_loan_repayment(
        self,
        school_id: str,
        payroll_run_id: str,
        staff_id: str,
        loan: StaffLoan,
    ) -> PayrollAdjustment:
        """Deduct one installment from a payroll run: creates a pre-approved
        PayrollAdjustment (adjustment_type='deduction_loan') that
        payroll_service._recompute_line_item_net folds into net_amount, and a
        StaffLoanRepayment audit row. Caller is responsible for calling
        _recompute_line_item_net afterwards."""
        installment = round(min(loan.installment_amount, loan.outstanding_balance), 2)

        adjustment = PayrollAdjustment(
            payroll_run_id=payroll_run_id,
            school_id=school_id,
            staff_id=staff_id,
            adjustment_type="deduction_loan",
            amount=-installment,
            reason=f"{loan.loan_type.capitalize()} repayment ({loan.installments_paid + 1}/{loan.total_installments})",
            created_by="SYSTEM",
            approved_by="SYSTEM",
            approved_at=datetime.utcnow(),
        )
        self.session.add(adjustment)
        await self.session.flush()

        loan.outstanding_balance = round(loan.outstanding_balance - installment, 2)
        loan.installments_paid += 1
        if loan.outstanding_balance <= 0.01:
            loan.status = LoanStatus.COMPLETED
        loan.updated_at = datetime.utcnow()
        self.session.add(loan)

        repayment = StaffLoanRepayment(
            school_id=school_id,
            loan_id=loan.id,
            payroll_run_id=payroll_run_id,
            payroll_adjustment_id=adjustment.id,
            staff_id=staff_id,
            installment_number=loan.installments_paid,
            amount_paid=installment,
            balance_after=loan.outstanding_balance,
        )
        self.session.add(repayment)
        await self.session.flush()

        return adjustment

    async def list_repayments(self, school_id: str, loan_id: str) -> List[StaffLoanRepayment]:
        result = await self.session.execute(
            select(StaffLoanRepayment).where(
                StaffLoanRepayment.school_id == school_id,
                StaffLoanRepayment.loan_id == loan_id,
            ).order_by(StaffLoanRepayment.created_at)
        )
        return result.scalars().all()
