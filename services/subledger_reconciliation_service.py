"""Sub-Ledger Reconciliation Service - Detail to control account matching

Handles:
- Sub-ledger detail import
- Detail to GL control account matching
- Aging analysis
- Variance detection
- Reconciliation completion
"""
import logging
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, and_, func
from datetime import datetime, timedelta

from models.finance.subledger_reconciliation import (
    SubLedgerReconciliation,
    SubLedgerType,
    SubLedgerStatus,
    SubLedgerDetail,
    DetailItemStatus,
    AgeingBucket,
    SubLedgerMatch,
    SubLedgerAdjustment,
    SubLedgerDetailCreate,
)
from models.finance import JournalEntry, JournalLineItem, PostingStatus
from models.finance.chart_of_accounts import GLAccount
from services.coa_service import CoaService

logger = logging.getLogger(__name__)


class SubLedgerReconciliationError(Exception):
    """Base exception for sub-ledger reconciliation operations"""
    pass


class SubLedgerReconciliationService:
    """Service for reconciling detail accounts to GL control accounts
    
    **Process**:
    1. Import detail account records (AR, AP, deposits, etc.)
    2. Sum detail balances and compare to GL control account
    3. Match individual detail records to GL postings
    4. Calculate aging (for AR/AP)
    5. Identify unmatched items
    6. Create adjustment entries for differences
    7. Mark reconciliation complete
    
    **Examples**:
    - AR Reconciliation: Match individual student AR records to GL AR control account
    - AP Reconciliation: Match vendor AP records to GL AP control account
    - Hostel Deposits: Match deposit records to GL deposits control account
    """
    
    def __init__(self, session: AsyncSession):
        """Initialize service
        
        Args:
            session: AsyncSession for database operations
        """
        self.session = session
        self.coa_service = CoaService(session)
    
    # ==================== Reconciliation Initialization ====================

    async def build_detail_records_from_subledger(
        self,
        school_id: str,
        subledger_type: SubLedgerType,
    ) -> List[SubLedgerDetailCreate]:
        """Auto-derive detail records straight from the Fee/Expense tables
        instead of requiring a caller to hand-key every student/vendor
        balance — the actual sub-ledger data already lives in those tables,
        so a reconciliation should read it from there.

        Only ACCOUNTS_RECEIVABLE (from Fee) and ACCOUNTS_PAYABLE (from
        Expense) are supported — the other SubLedgerType values (hostel
        deposits, employee advances) have no single owning table this
        service can assume, so those still take hand-built detail_records.
        """
        if subledger_type == SubLedgerType.ACCOUNTS_RECEIVABLE:
            from models.fee import Fee, PaymentStatus as FeePaymentStatus
            from models.student import Student

            result = await self.session.execute(
                select(Fee, Student).join(Student, Fee.student_id == Student.id).where(
                    and_(
                        Fee.school_id == school_id,
                        Fee.status.notin_([FeePaymentStatus.PAID, FeePaymentStatus.WRITTEN_OFF]),
                    )
                )
            )
            records = []
            for fee, student in result.all():
                balance = fee.amount_due - fee.discount - fee.amount_paid
                if balance <= 0.01:
                    continue
                records.append(
                    SubLedgerDetailCreate(
                        detail_reference_id=student.id,
                        reference_type="STUDENT",
                        detail_description=f"{student.first_name} {student.last_name} — Fee {fee.id}",
                        detail_balance=balance,
                        last_transaction_date=fee.updated_at,
                    )
                )
            return records

        if subledger_type == SubLedgerType.ACCOUNTS_PAYABLE:
            from models.finance.expenses import Expense, ExpenseStatus

            result = await self.session.execute(
                select(Expense).where(
                    and_(
                        Expense.school_id == school_id,
                        Expense.status == ExpenseStatus.POSTED,
                    )
                )
            )
            records = []
            for expense in result.scalars().all():
                balance = float(expense.amount - expense.amount_paid)
                if balance <= 0.01:
                    continue
                records.append(
                    SubLedgerDetailCreate(
                        detail_reference_id=expense.vendor_id or expense.id,
                        reference_type="VENDOR",
                        detail_description=f"{expense.vendor_name or 'Unnamed payee'} — Expense {expense.id}",
                        detail_balance=balance,
                        last_transaction_date=expense.expense_date,
                    )
                )
            return records

        raise SubLedgerReconciliationError(
            f"Auto-generation isn't supported for {subledger_type.value}; "
            "pass detail_records explicitly instead."
        )

    async def create_subledger_reconciliation(
        self,
        school_id: str,
        subledger_type: SubLedgerType,
        control_account_id: str,
        detail_records: List[SubLedgerDetailCreate],
        reconciled_by: str,
        notes: Optional[str] = None,
    ) -> str:
        """Create a sub-ledger reconciliation and import detail records
        
        Args:
            school_id: School identifier
            subledger_type: Type of sub-ledger (AR, AP, etc.)
            control_account_id: GL control account ID
            detail_records: List of detail account records
            reconciled_by: User creating reconciliation
            notes: Optional notes
            
        Returns:
            SubLedgerReconciliation ID
            
        Raises:
            SubLedgerReconciliationError: If creation fails
        """
        try:
            # Verify GL control account exists
            control_account = await self.coa_service.get_account_by_id(
                school_id,
                control_account_id
            )
            if not control_account:
                raise SubLedgerReconciliationError(
                    f"GL Control Account {control_account_id} not found"
                )
            
            # Calculate detail total
            detail_total = sum(rec.detail_balance for rec in detail_records)
            
            # Create reconciliation record
            reconciliation = SubLedgerReconciliation(
                school_id=school_id,
                subledger_type=subledger_type,
                control_account_id=control_account_id,
                reconciliation_date=datetime.utcnow(),
                reconciliation_status=SubLedgerStatus.IN_PROGRESS,
                total_detail_records=len(detail_records),
                detail_total_balance=detail_total,
                # control_account.current_balance is Decimal; this model's
                # balance fields are still float (out of scope for the
                # Decimal migration), so convert at this boundary.
                gl_control_balance=float(control_account.current_balance),
                reconciled_by=reconciled_by,
                notes=notes,
            )
            
            self.session.add(reconciliation)
            await self.session.flush()
            
            # Import detail records
            for detail_rec in detail_records:
                # Calculate aging bucket
                days_outstanding = (datetime.utcnow() - detail_rec.last_transaction_date).days
                
                if days_outstanding <= 30:
                    aging_bucket = AgeingBucket.CURRENT
                elif days_outstanding <= 60:
                    aging_bucket = AgeingBucket.THIRTY_TO_SIXTY
                elif days_outstanding <= 90:
                    aging_bucket = AgeingBucket.SIXTY_TO_NINETY
                else:
                    aging_bucket = AgeingBucket.OVER_NINETY
                
                detail = SubLedgerDetail(
                    school_id=school_id,
                    subledger_reconciliation_id=reconciliation.id,
                    detail_reference_id=detail_rec.detail_reference_id,
                    reference_type=detail_rec.reference_type,
                    detail_description=detail_rec.detail_description,
                    detail_balance=detail_rec.detail_balance,
                    last_transaction_date=detail_rec.last_transaction_date,
                    match_status=DetailItemStatus.PENDING,
                    aging_bucket=aging_bucket,
                    days_outstanding=days_outstanding,
                    matched_by=reconciled_by,
                )
                self.session.add(detail)
            
            await self.session.commit()
            
            logger.info(
                f"Created {subledger_type.value} sub-ledger reconciliation "
                f"with {len(detail_records)} detail records (total: {detail_total:.2f})"
            )
            
            return reconciliation.id
            
        except SubLedgerReconciliationError:
            await self.session.rollback()
            raise
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error creating sub-ledger reconciliation: {str(e)}")
            raise SubLedgerReconciliationError(
                f"Failed to create sub-ledger reconciliation: {str(e)}"
            )
    
    # ==================== Automatic Matching ====================
    
    async def auto_match_details(
        self,
        school_id: str,
        reconciliation_id: str,
    ) -> Dict[str, Any]:
        """Automatically match detail records to GL postings
        
        Matching strategy:
        1. Group GL entries by journal entry
        2. For each detail record, find matching GL posting (similar amount/date)
        3. Mark as matched if found, unmatched if not
        
        Args:
            school_id: School identifier
            reconciliation_id: SubLedgerReconciliation ID
            
        Returns:
            Matching summary
            
        Raises:
            SubLedgerReconciliationError: If matching fails
        """
        try:
            # Get reconciliation — school_id scoped, so a caller can't act on
            # another school's reconciliation by passing its id.
            recon = await self.session.execute(
                select(SubLedgerReconciliation).where(
                    SubLedgerReconciliation.id == reconciliation_id,
                    SubLedgerReconciliation.school_id == school_id,
                )
            )
            reconciliation = recon.scalar_one_or_none()
            if not reconciliation:
                raise SubLedgerReconciliationError(
                    f"Reconciliation {reconciliation_id} not found"
                )

            # Get all detail records
            detail_result = await self.session.execute(
                select(SubLedgerDetail).where(
                    SubLedgerDetail.subledger_reconciliation_id == reconciliation_id,
                    SubLedgerDetail.school_id == school_id,
                )
            )
            detail_records = detail_result.scalars().all()
            
            # Get GL entries that actually posted to THIS control account
            # (last 90 days) — previously this matched against ANY posted
            # entry school-wide using the entry's total_debit header total,
            # so a completely unrelated entry could match purely because its
            # total and date coincided with a detail record. Joining to
            # JournalLineItem filtered by gl_account_id and using that
            # line's own amount is what actually validates against the
            # control account this reconciliation claims to reconcile.
            cutoff_date = datetime.utcnow() - timedelta(days=90)
            gl_result = await self.session.execute(
                select(JournalEntry, JournalLineItem)
                .join(JournalLineItem, JournalLineItem.journal_entry_id == JournalEntry.id)
                .where(
                    and_(
                        JournalEntry.school_id == school_id,
                        JournalEntry.posting_status == PostingStatus.POSTED,
                        JournalEntry.entry_date >= cutoff_date,
                        JournalLineItem.gl_account_id == reconciliation.control_account_id,
                    )
                )
            )
            gl_entries = [
                (entry, float(line.debit_amount or line.credit_amount))
                for entry, line in gl_result.all()
            ]

            matched_count = 0
            unmatched_count = 0
            variance_count = 0

            # Try to match each detail record
            for detail in detail_records:
                match_found = False

                # Try exact match (amount + date)
                for gl_entry, gl_amount in gl_entries:
                    if abs(detail.detail_balance - gl_amount) < 0.01 and \
                       abs((detail.last_transaction_date - gl_entry.entry_date).days) <= 1:

                        match = SubLedgerMatch(
                            school_id=school_id,
                            subledger_reconciliation_id=reconciliation_id,
                            subledger_detail_id=detail.id,
                            journal_entry_id=gl_entry.id,
                            detail_amount=detail.detail_balance,
                            gl_amount=gl_amount,
                            variance_amount=0.0,
                            detail_date=detail.last_transaction_date,
                            gl_date=gl_entry.entry_date,
                            is_exact_match=True,
                            matched_by="SYSTEM",
                        )
                        self.session.add(match)

                        detail.match_status = DetailItemStatus.MATCHED
                        detail.journal_entry_id = gl_entry.id
                        detail.gl_posted_date = gl_entry.entry_date
                        detail.gl_posted_amount = gl_amount
                        detail.matched_by = "SYSTEM"

                        match_found = True
                        matched_count += 1
                        break

                # If no exact match, try within tolerance
                if not match_found:
                    for gl_entry, gl_amount in gl_entries:
                        variance = abs(detail.detail_balance - gl_amount)
                        if variance < 1.00:  # Within $1.00

                            match = SubLedgerMatch(
                                school_id=school_id,
                                subledger_reconciliation_id=reconciliation_id,
                                subledger_detail_id=detail.id,
                                journal_entry_id=gl_entry.id,
                                detail_amount=detail.detail_balance,
                                gl_amount=gl_amount,
                                variance_amount=variance,
                                detail_date=detail.last_transaction_date,
                                gl_date=gl_entry.entry_date,
                                days_variance=(gl_entry.entry_date - detail.last_transaction_date).days,
                                requires_review=True,
                                matched_by="SYSTEM",
                            )
                            self.session.add(match)

                            detail.match_status = DetailItemStatus.VARIANCE
                            detail.journal_entry_id = gl_entry.id
                            detail.gl_posted_date = gl_entry.entry_date
                            detail.gl_posted_amount = gl_amount
                            detail.variance_amount = variance
                            detail.matched_by = "SYSTEM"

                            match_found = True
                            variance_count += 1
                            break
                
                # If still no match, mark as unmatched
                if not match_found:
                    detail.match_status = DetailItemStatus.UNMATCHED
                    unmatched_count += 1
            
            await self.session.commit()
            
            # Update reconciliation totals
            reconciliation.matched_records = matched_count
            reconciliation.variance_records = variance_count
            reconciliation.unmatched_records = unmatched_count
            self.session.add(reconciliation)
            await self.session.commit()
            
            return {
                "reconciliation_id": reconciliation_id,
                "matched": matched_count,
                "variance": variance_count,
                "unmatched": unmatched_count,
                "total": len(detail_records),
            }
            
        except SubLedgerReconciliationError:
            raise
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error in auto-matching: {str(e)}")
            raise SubLedgerReconciliationError(f"Failed to match details: {str(e)}")
    
    # ==================== Aging Analysis ====================
    
    async def calculate_aging_analysis(
        self,
        school_id: str,
        reconciliation_id: str,
    ) -> Dict[str, Any]:
        """Calculate aging analysis for AR/AP reconciliation
        
        Breaks down balances by aging bucket.
        
        Args:
            school_id: School identifier
            reconciliation_id: SubLedgerReconciliation ID
            
        Returns:
            Aging analysis by bucket
        """
        try:
            # Get detail records
            detail_result = await self.session.execute(
                select(SubLedgerDetail).where(
                    SubLedgerDetail.subledger_reconciliation_id == reconciliation_id,
                    SubLedgerDetail.school_id == school_id,
                )
            )
            detail_records = detail_result.scalars().all()

            current = 0.0
            thirty_to_sixty = 0.0
            sixty_to_ninety = 0.0
            over_ninety = 0.0
            
            for detail in detail_records:
                if detail.aging_bucket == AgeingBucket.CURRENT:
                    current += detail.detail_balance
                elif detail.aging_bucket == AgeingBucket.THIRTY_TO_SIXTY:
                    thirty_to_sixty += detail.detail_balance
                elif detail.aging_bucket == AgeingBucket.SIXTY_TO_NINETY:
                    sixty_to_ninety += detail.detail_balance
                elif detail.aging_bucket == AgeingBucket.OVER_NINETY:
                    over_ninety += detail.detail_balance
            
            # Update reconciliation with aging data
            recon = await self.session.execute(
                select(SubLedgerReconciliation).where(
                    SubLedgerReconciliation.id == reconciliation_id,
                    SubLedgerReconciliation.school_id == school_id,
                )
            )
            reconciliation = recon.scalar_one_or_none()
            if not reconciliation:
                raise SubLedgerReconciliationError(
                    f"Reconciliation {reconciliation_id} not found"
                )

            reconciliation.current_balance = current
            reconciliation.thirty_to_sixty_balance = thirty_to_sixty
            reconciliation.sixty_to_ninety_balance = sixty_to_ninety
            reconciliation.over_ninety_balance = over_ninety
            
            self.session.add(reconciliation)
            await self.session.commit()
            
            return {
                "reconciliation_id": reconciliation_id,
                "current_0_30_days": current,
                "thirty_to_60_days": thirty_to_sixty,
                "sixty_to_90_days": sixty_to_ninety,
                "over_90_days": over_ninety,
                "total_balance": current + thirty_to_sixty + sixty_to_ninety + over_ninety,
            }
            
        except Exception as e:
            logger.error(f"Error calculating aging: {str(e)}")
            raise SubLedgerReconciliationError(f"Failed to calculate aging: {str(e)}")
    
    # ==================== Variance Analysis ====================
    
    async def calculate_variance(
        self,
        school_id: str,
        reconciliation_id: str,
    ) -> Dict[str, Any]:
        """Calculate variance between detail total and GL control account
        
        Args:
            school_id: School identifier
            reconciliation_id: SubLedgerReconciliation ID
            
        Returns:
            Variance analysis
        """
        try:
            recon = await self.session.execute(
                select(SubLedgerReconciliation).where(
                    SubLedgerReconciliation.id == reconciliation_id,
                    SubLedgerReconciliation.school_id == school_id,
                )
            )
            reconciliation = recon.scalar_one_or_none()
            if not reconciliation:
                raise SubLedgerReconciliationError(
                    f"Reconciliation {reconciliation_id} not found"
                )

            # Recalculate detail total
            detail_result = await self.session.execute(
                select(func.sum(SubLedgerDetail.detail_balance)).where(
                    SubLedgerDetail.subledger_reconciliation_id == reconciliation_id,
                    SubLedgerDetail.school_id == school_id,
                )
            )
            detail_total = float(detail_result.scalar() or 0.0)
            
            # Get GL control balance (current_balance is Decimal; this
            # subsystem's own fields are still float)
            control_account = await self.coa_service.get_account_by_id(
                school_id,
                reconciliation.control_account_id
            )
            gl_balance = float(control_account.current_balance) if control_account else 0.0
            
            variance = detail_total - gl_balance
            
            reconciliation.detail_total_balance = detail_total
            reconciliation.gl_control_balance = gl_balance
            reconciliation.total_variance = variance
            
            if abs(variance) < 0.01:
                reconciliation.variance_reason = "Reconciled"
            else:
                reconciliation.variance_reason = f"Variance of {variance:.2f} to reconcile"
            
            self.session.add(reconciliation)
            await self.session.commit()
            
            return {
                "reconciliation_id": reconciliation_id,
                "detail_total": detail_total,
                "gl_control_balance": gl_balance,
                "variance": variance,
                "is_balanced": abs(variance) < 0.01,
            }
            
        except SubLedgerReconciliationError:
            raise
        except Exception as e:
            logger.error(f"Error calculating variance: {str(e)}")
            raise SubLedgerReconciliationError(f"Failed to calculate variance: {str(e)}")
    
    # ==================== Reconciliation Completion ====================
    
    async def complete_reconciliation(
        self,
        school_id: str,
        reconciliation_id: str,
        approved_by: str,
    ) -> Dict[str, Any]:
        """Mark sub-ledger reconciliation as completed
        
        Can only complete if variance is zero (or very close due to rounding).
        
        Args:
            school_id: School identifier
            reconciliation_id: SubLedgerReconciliation ID
            approved_by: User approving
            
        Returns:
            Completion summary
            
        Raises:
            SubLedgerReconciliationError: If cannot complete
        """
        try:
            recon = await self.session.execute(
                select(SubLedgerReconciliation).where(
                    SubLedgerReconciliation.id == reconciliation_id,
                    SubLedgerReconciliation.school_id == school_id,
                )
            )
            reconciliation = recon.scalar_one_or_none()
            if not reconciliation:
                raise SubLedgerReconciliationError(
                    f"Reconciliation {reconciliation_id} not found"
                )

            # Check if balanced
            variance_result = await self.calculate_variance(school_id, reconciliation_id)
            if not variance_result["is_balanced"]:
                raise SubLedgerReconciliationError(
                    f"Cannot complete reconciliation with variance of {variance_result['variance']:.2f}"
                )
            
            # Mark as completed
            reconciliation.reconciliation_status = SubLedgerStatus.COMPLETED
            reconciliation.approved_by = approved_by
            reconciliation.approved_date = datetime.utcnow()
            
            self.session.add(reconciliation)
            await self.session.commit()
            
            logger.info(
                f"Completed sub-ledger reconciliation {reconciliation_id} "
                f"({reconciliation.subledger_type.value})"
            )
            
            return {
                "status": "success",
                "reconciliation_id": reconciliation_id,
                "completed_date": reconciliation.approved_date.isoformat(),
                "approved_by": approved_by,
            }
            
        except SubLedgerReconciliationError:
            raise
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error completing reconciliation: {str(e)}")
            raise SubLedgerReconciliationError(f"Failed to complete reconciliation: {str(e)}")
    
    # ==================== Reporting ====================
    
    async def get_reconciliation_summary(
        self,
        school_id: str,
        reconciliation_id: str,
    ) -> Dict[str, Any]:
        """Get complete sub-ledger reconciliation summary
        
        Args:
            school_id: School identifier
            reconciliation_id: SubLedgerReconciliation ID
            
        Returns:
            Comprehensive reconciliation report
        """
        try:
            recon = await self.session.execute(
                select(SubLedgerReconciliation).where(
                    SubLedgerReconciliation.id == reconciliation_id,
                    SubLedgerReconciliation.school_id == school_id,
                )
            )
            reconciliation = recon.scalar_one_or_none()
            if not reconciliation:
                raise SubLedgerReconciliationError(
                    f"Reconciliation {reconciliation_id} not found"
                )

            variance = await self.calculate_variance(school_id, reconciliation_id)
            aging = await self.calculate_aging_analysis(school_id, reconciliation_id)
            
            return {
                "reconciliation_id": reconciliation_id,
                "subledger_type": reconciliation.subledger_type.value,
                "control_account_id": reconciliation.control_account_id,
                "reconciliation_date": reconciliation.reconciliation_date.isoformat(),
                "status": reconciliation.reconciliation_status.value,
                "detail_total": variance["detail_total"],
                "gl_control_balance": variance["gl_control_balance"],
                "variance": variance["variance"],
                "is_balanced": variance["is_balanced"],
                "matched_records": reconciliation.matched_records,
                "variance_records": reconciliation.variance_records,
                "unmatched_records": reconciliation.unmatched_records,
                "total_records": reconciliation.total_detail_records,
                "aging": aging,
                "reconciled_by": reconciliation.reconciled_by,
                "approved_by": reconciliation.approved_by,
            }
            
        except SubLedgerReconciliationError:
            raise
        except Exception as e:
            logger.error(f"Error generating summary: {str(e)}")
            return {"error": str(e)}
    
    async def get_unmatched_details(
        self,
        school_id: str,
        reconciliation_id: str,
    ) -> List[Dict[str, Any]]:
        """Get unmatched detail records
        
        Args:
            school_id: School identifier
            reconciliation_id: SubLedgerReconciliation ID
            
        Returns:
            List of unmatched detail records
        """
        try:
            result = await self.session.execute(
                select(SubLedgerDetail).where(
                    and_(
                        SubLedgerDetail.subledger_reconciliation_id == reconciliation_id,
                        SubLedgerDetail.school_id == school_id,
                        SubLedgerDetail.match_status.in_([
                            DetailItemStatus.UNMATCHED,
                            DetailItemStatus.VARIANCE,
                        ])
                    )
                )
            )
            
            unmatched = result.scalars().all()
            
            return [
                {
                    "id": d.id,
                    "detail_reference_id": d.detail_reference_id,
                    "description": d.detail_description,
                    "balance": d.detail_balance,
                    "status": d.match_status.value,
                    "variance": d.variance_amount,
                    "aging_bucket": d.aging_bucket.value if d.aging_bucket else None,
                }
                for d in unmatched
            ]
            
        except Exception as e:
            logger.error(f"Error getting unmatched details: {str(e)}")
            return []
