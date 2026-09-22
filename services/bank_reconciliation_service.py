"""Bank Reconciliation Service - GL to bank statement matching

Handles:
- Bank statement import
- Automatic GL to bank matching
- Outstanding item tracking
- Reconciliation variance analysis
- GL adjustment entry creation
"""
import logging
from typing import Optional, List, Dict, Any, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, and_, or_, func
from datetime import datetime, timedelta

from models.finance.bank_reconciliation import (
    BankReconciliation,
    BankReconciliationStatus,
    BankStatement,
    BankStatementCreate,
    BankTransactionType,
    BankReconciliationMatch,
    BankItemStatus,
    BankReconciliationAdjustment,
)
from models.finance import JournalEntry, JournalLineItem, PostingStatus
from models.finance.chart_of_accounts import GLAccount
from services.coa_service import CoaService
from services.journal_entry_service import JournalEntryService

logger = logging.getLogger(__name__)


class BankReconciliationError(Exception):
    """Base exception for bank reconciliation operations"""
    pass


class BankReconciliationService:
    """Service for GL bank account reconciliation with bank statements
    
    **Process**:
    1. Import bank statement transactions
    2. Automatically match GL entries to bank transactions
    3. Identify unmatched items (outstanding, deposits in transit)
    4. Calculate reconciling items
    5. Create GL adjustments for differences (fees, interest)
    6. Mark reconciliation complete
    
    **Key Concepts**:
    - Outstanding Checks: GL entries with no bank match (checks not cleared)
    - Deposits in Transit: Bank deposits not yet in GL
    - Bank Fees: Bank charges requiring GL adjustment
    - Reconciling Items: Timing differences that should clear over time
    """
    
    def __init__(self, session: AsyncSession):
        """Initialize service
        
        Args:
            session: AsyncSession for database operations
        """
        self.session = session
        self.coa_service = CoaService(session)
        self.journal_service = JournalEntryService(session)
    
    # ==================== Bank Statement Import ====================
    
    async def import_bank_statement(
        self,
        school_id: str,
        gl_account_id: str,
        statement_date: datetime,
        statement_beginning_balance: float,
        statement_ending_balance: float,
        transactions: List[BankStatementCreate],
        reconciled_by: str,
        notes: Optional[str] = None,
    ) -> str:
        """Import a bank statement and create reconciliation record
        
        Args:
            school_id: School identifier
            gl_account_id: GL bank account being reconciled
            statement_date: Date of bank statement
            statement_beginning_balance: Starting balance
            statement_ending_balance: Ending balance
            transactions: List of bank transactions
            reconciled_by: User importing statement
            notes: Optional reconciliation notes
            
        Returns:
            BankReconciliation ID
            
        Raises:
            BankReconciliationError: If import fails
        """
        try:
            # Get GL account
            gl_account = await self.coa_service.get_account_by_id(school_id, gl_account_id)
            if not gl_account:
                raise BankReconciliationError(f"GL Account {gl_account_id} not found")
            
            # Create reconciliation record
            reconciliation = BankReconciliation(
                school_id=school_id,
                gl_account_id=gl_account_id,
                statement_date=statement_date,
                statement_beginning_balance=statement_beginning_balance,
                statement_ending_balance=statement_ending_balance,
                # gl_account.current_balance is Decimal; this model's balance
                # fields are still float (bank reconciliation wasn't part of
                # the Decimal migration), so convert at this boundary.
                gl_beginning_balance=float(gl_account.current_balance),
                gl_ending_balance=float(gl_account.current_balance),
                reconciliation_date=datetime.utcnow(),
                reconciliation_status=BankReconciliationStatus.IN_PROGRESS,
                total_bank_transactions=len(transactions),
                reconciled_by=reconciled_by,
                notes=notes,
            )
            
            self.session.add(reconciliation)
            await self.session.flush()
            
            # Import bank statement transactions
            for idx, txn in enumerate(transactions):
                bank_statement = BankStatement(
                    school_id=school_id,
                    bank_reconciliation_id=reconciliation.id,
                    statement_line_number=idx + 1,
                    transaction_date=txn.transaction_date,
                    transaction_type=txn.transaction_type,
                    description=txn.description,
                    amount=txn.amount,
                    running_balance=txn.running_balance,
                    bank_reference=txn.bank_reference,
                    imported_by=reconciled_by,
                )
                self.session.add(bank_statement)
            
            await self.session.commit()
            
            logger.info(
                f"Imported bank statement for account {gl_account.account_code} "
                f"with {len(transactions)} transactions"
            )
            
            return reconciliation.id
            
        except BankReconciliationError:
            await self.session.rollback()
            raise
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error importing bank statement: {str(e)}")
            raise BankReconciliationError(f"Failed to import bank statement: {str(e)}")
    
    # ==================== Automatic Matching ====================
    
    async def auto_match_transactions(
        self,
        school_id: str,
        reconciliation_id: str,
        matching_window_days: int = 5,
    ) -> Dict[str, Any]:
        """Automatically match bank transactions to GL entries
        
        Matching rules (in order of priority):
        1. Exact match: Same amount + same date
        2. Date within window: Same amount, date within matching_window_days
        3. Close amount: Amount within $0.01, date within window
        4. Outstanding check: GL entry > bank transaction date (check not cleared)
        5. Deposit in transit: Bank deposit > GL entry date (not yet recorded)
        
        Args:
            school_id: School identifier
            reconciliation_id: BankReconciliation ID
            matching_window_days: Number of days to match within
            
        Returns:
            Summary of matching results
            
        Raises:
            BankReconciliationError: If matching fails
        """
        try:
            # Get reconciliation — school_id scopes this to the caller's own
            # tenant; without it, any school's finance user could act on
            # another school's reconciliation by passing its id.
            recon = await self.session.execute(
                select(BankReconciliation).where(
                    BankReconciliation.id == reconciliation_id,
                    BankReconciliation.school_id == school_id,
                )
            )
            reconciliation = recon.scalar_one_or_none()
            if not reconciliation:
                raise BankReconciliationError(f"Reconciliation {reconciliation_id} not found")

            # Idempotency guard: bank items and GL entries that already have a
            # match record (of any status) from a prior run are skipped, so
            # re-running auto-match doesn't create duplicate match rows or
            # re-match a GL entry that's already spoken for.
            existing_matches_result = await self.session.execute(
                select(BankReconciliationMatch.bank_statement_id, BankReconciliationMatch.journal_entry_id).where(
                    BankReconciliationMatch.bank_reconciliation_id == reconciliation_id
                )
            )
            existing_rows = existing_matches_result.all()
            already_matched_bank_ids = {row[0] for row in existing_rows}
            used_gl_ids = {row[1] for row in existing_rows if row[1]}

            # Get all bank statement items not already processed
            bank_result = await self.session.execute(
                select(BankStatement).where(
                    BankStatement.bank_reconciliation_id == reconciliation_id
                ).order_by(BankStatement.transaction_date)
            )
            bank_items = [
                b for b in bank_result.scalars().all() if b.id not in already_matched_bank_ids
            ]

            # Get GL entries that actually posted to THIS bank account (last
            # 60 days for matching window) — previously this matched against
            # ANY posted entry school-wide using the entry's total_debit
            # header total, so e.g. a $12,000 payroll entry (nothing to do
            # with this bank account) could get matched to a $12,000 bank
            # deposit purely because the totals and dates coincided. Joining
            # to JournalLineItem filtered by gl_account_id and using that
            # line's own amount is what actually validates the ledger this
            # reconciliation claims to reconcile.
            cutoff_date = reconciliation.statement_date - timedelta(days=60)
            gl_result = await self.session.execute(
                select(JournalEntry, JournalLineItem)
                .join(JournalLineItem, JournalLineItem.journal_entry_id == JournalEntry.id)
                .where(
                    and_(
                        JournalEntry.school_id == school_id,
                        JournalEntry.posting_status == PostingStatus.POSTED,
                        JournalEntry.entry_date >= cutoff_date,
                        JournalLineItem.gl_account_id == reconciliation.gl_account_id,
                    )
                )
            )
            # (entry, line_amount) pairs — line_amount is whichever of
            # debit_amount/credit_amount is nonzero on the line that touched
            # this specific account (a line is one or the other, never both).
            gl_entries = [
                (entry, float(line.debit_amount or line.credit_amount))
                for entry, line in gl_result.all()
            ]

            matched_count = 0
            unmatched_bank = 0

            # For each not-yet-processed bank item, try to find an unused GL match
            for bank_item in bank_items:
                match_found = False

                # Try exact match (amount + date)
                for gl_entry, gl_amount in gl_entries:
                    if gl_entry.id in used_gl_ids:
                        continue
                    if abs(bank_item.amount - gl_amount) < 0.01 and \
                       bank_item.transaction_date.date() == gl_entry.entry_date.date():

                        match = BankReconciliationMatch(
                            school_id=school_id,
                            bank_reconciliation_id=reconciliation_id,
                            bank_statement_id=bank_item.id,
                            journal_entry_id=gl_entry.id,
                            match_status=BankItemStatus.MATCHED,
                            bank_amount=bank_item.amount,
                            bank_date=bank_item.transaction_date,
                            bank_description=bank_item.description,
                            gl_amount=gl_amount,
                            gl_date=gl_entry.entry_date,
                            gl_description=gl_entry.description,
                            matched_by=reconciliation.reconciled_by,
                        )
                        self.session.add(match)
                        bank_item.is_cleared = True
                        bank_item.cleared_date = datetime.utcnow()
                        used_gl_ids.add(gl_entry.id)
                        match_found = True
                        matched_count += 1
                        break

                # If no exact match, try within matching window
                if not match_found:
                    window_start = bank_item.transaction_date - timedelta(days=matching_window_days)
                    window_end = bank_item.transaction_date + timedelta(days=matching_window_days)

                    for gl_entry, gl_amount in gl_entries:
                        if gl_entry.id in used_gl_ids:
                            continue
                        if abs(bank_item.amount - gl_amount) < 0.01 and \
                           window_start <= gl_entry.entry_date <= window_end:

                            days_diff = (gl_entry.entry_date - bank_item.transaction_date).days

                            match = BankReconciliationMatch(
                                school_id=school_id,
                                bank_reconciliation_id=reconciliation_id,
                                bank_statement_id=bank_item.id,
                                journal_entry_id=gl_entry.id,
                                match_status=BankItemStatus.MATCHED,
                                bank_amount=bank_item.amount,
                                bank_date=bank_item.transaction_date,
                                bank_description=bank_item.description,
                                gl_amount=gl_amount,
                                gl_date=gl_entry.entry_date,
                                gl_description=gl_entry.description,
                                days_variance=days_diff,
                                matched_by=reconciliation.reconciled_by,
                            )
                            self.session.add(match)
                            bank_item.is_cleared = True
                            bank_item.cleared_date = datetime.utcnow()
                            used_gl_ids.add(gl_entry.id)
                            match_found = True
                            matched_count += 1
                            break

                # If still no match, create unmatched item
                if not match_found:
                    match = BankReconciliationMatch(
                        school_id=school_id,
                        bank_reconciliation_id=reconciliation_id,
                        bank_statement_id=bank_item.id,
                        match_status=BankItemStatus.UNMATCHED_BANK,
                        bank_amount=bank_item.amount,
                        bank_date=bank_item.transaction_date,
                        bank_description=bank_item.description,
                        requires_review=True,
                        matched_by=reconciliation.reconciled_by,
                    )
                    self.session.add(match)
                    unmatched_bank += 1

            await self.session.commit()

            # GL entries dated in the matching window that never got claimed
            # by any bank item, across the whole reconciliation (not just this run)
            unmatched_gl = len([g for g, _ in gl_entries if g.id not in used_gl_ids])

            # Recompute totals from the full set of match records (not just
            # this run's additions), so reconciliation stats stay correct
            # across repeated auto-match calls.
            total_matched_result = await self.session.execute(
                select(func.count()).select_from(BankReconciliationMatch).where(
                    and_(
                        BankReconciliationMatch.bank_reconciliation_id == reconciliation_id,
                        BankReconciliationMatch.match_status == BankItemStatus.MATCHED,
                    )
                )
            )
            total_matched = total_matched_result.scalar() or 0

            total_unmatched_bank_result = await self.session.execute(
                select(func.count()).select_from(BankReconciliationMatch).where(
                    and_(
                        BankReconciliationMatch.bank_reconciliation_id == reconciliation_id,
                        BankReconciliationMatch.match_status == BankItemStatus.UNMATCHED_BANK,
                    )
                )
            )
            total_unmatched_bank = total_unmatched_bank_result.scalar() or 0

            # Update reconciliation totals
            reconciliation.matched_transactions = total_matched
            reconciliation.unmatched_bank_items = total_unmatched_bank
            reconciliation.unmatched_gl_items = unmatched_gl
            self.session.add(reconciliation)
            await self.session.commit()

            return {
                "reconciliation_id": reconciliation_id,
                "matched": total_matched,
                "unmatched_bank": total_unmatched_bank,
                "unmatched_gl": unmatched_gl,
                "newly_matched_this_run": matched_count,
                "skipped_already_processed": len(already_matched_bank_ids),
            }

        except BankReconciliationError:
            raise
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error in automatic matching: {str(e)}")
            raise BankReconciliationError(f"Failed to match transactions: {str(e)}")
    
    # ==================== Manual Matching ====================
    
    async def manually_match_transaction(
        self,
        school_id: str,
        bank_statement_id: str,
        journal_entry_id: str,
        matched_by: str,
        variance_reason: Optional[str] = None,
    ) -> BankReconciliationMatch:
        """Manually match a bank transaction to GL entry
        
        Used for non-automatic matches that require user review.
        
        Args:
            school_id: School identifier
            bank_statement_id: Bank statement item ID
            journal_entry_id: Journal entry ID
            matched_by: User performing match
            variance_reason: Reason if amounts differ
            
        Returns:
            Created BankReconciliationMatch
        """
        try:
            # Get bank statement — school_id scoped, so this match can never
            # be persisted (below) tagging a different school's record with
            # the caller's own school_id.
            bank_result = await self.session.execute(
                select(BankStatement).where(
                    BankStatement.id == bank_statement_id,
                    BankStatement.school_id == school_id,
                )
            )
            bank_item = bank_result.scalar_one_or_none()
            if not bank_item:
                raise BankReconciliationError(f"Bank statement {bank_statement_id} not found")

            # Idempotency guard: this bank line must not already have a match
            # record — auto_match_transactions already has this check, but
            # manual matching never did, so the same statement line could be
            # matched to two different GL entries with nothing stopping it
            # (no DB unique constraint on bank_statement_id either), silently
            # inflating matched_transactions and double-counting the item in
            # reconciliation totals. There's no "unmatch" endpoint, so a
            # legitimate re-match scenario doesn't currently exist.
            existing_match = await self.session.execute(
                select(BankReconciliationMatch).where(BankReconciliationMatch.bank_statement_id == bank_statement_id)
            )
            if existing_match.scalar_one_or_none():
                raise BankReconciliationError(f"Bank statement {bank_statement_id} already has a match recorded")

            # Get GL entry — same school_id scoping
            gl_result = await self.session.execute(
                select(JournalEntry).where(
                    JournalEntry.id == journal_entry_id,
                    JournalEntry.school_id == school_id,
                )
            )
            gl_entry = gl_result.scalar_one_or_none()
            if not gl_entry:
                raise BankReconciliationError(f"Journal entry {journal_entry_id} not found")

            # The entry must have actually posted to the account being
            # reconciled — otherwise a completely unrelated entry (matching
            # totals by coincidence) could be linked as if validated against
            # this bank account's own ledger. Uses that line's own
            # debit/credit amount (whichever is nonzero) rather than the
            # entry's total_debit header, which reflects the whole entry,
            # not just the piece that touched this account.
            recon_result = await self.session.execute(
                select(BankReconciliation).where(BankReconciliation.id == bank_item.bank_reconciliation_id)
            )
            reconciliation = recon_result.scalar_one_or_none()
            if not reconciliation:
                raise BankReconciliationError("Reconciliation not found for this bank statement")

            line_result = await self.session.execute(
                select(JournalLineItem).where(
                    JournalLineItem.journal_entry_id == journal_entry_id,
                    JournalLineItem.gl_account_id == reconciliation.gl_account_id,
                )
            )
            line_item = line_result.scalar_one_or_none()
            if not line_item:
                raise BankReconciliationError(
                    f"Journal entry {journal_entry_id} has no posting to the account being reconciled"
                )
            gl_amount = float(line_item.debit_amount or line_item.credit_amount)

            # Calculate variance
            variance = bank_item.amount - gl_amount

            # Create match
            match = BankReconciliationMatch(
                school_id=school_id,
                bank_reconciliation_id=bank_item.bank_reconciliation_id,
                bank_statement_id=bank_statement_id,
                journal_entry_id=journal_entry_id,
                match_status=BankItemStatus.MATCHED if abs(variance) < 0.01 else BankItemStatus.PENDING,
                bank_amount=bank_item.amount,
                bank_date=bank_item.transaction_date,
                bank_description=bank_item.description,
                gl_amount=gl_amount,
                gl_date=gl_entry.entry_date,
                gl_description=gl_entry.description,
                variance_amount=variance,
                variance_reason=variance_reason,
                days_variance=(gl_entry.entry_date - bank_item.transaction_date).days,
                matched_by=matched_by,
            )
            
            self.session.add(match)
            bank_item.is_cleared = True
            bank_item.cleared_date = datetime.utcnow()
            await self.session.commit()
            
            return match
            
        except BankReconciliationError:
            raise
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error in manual matching: {str(e)}")
            raise BankReconciliationError(f"Failed to match transactions: {str(e)}")
    
    # ==================== Reconciliation Analysis ====================
    
    async def calculate_reconciling_items(
        self,
        school_id: str,
        reconciliation_id: str,
    ) -> Dict[str, Any]:
        """Calculate reconciling items (outstanding checks, deposits in transit)
        
        These are timing differences that explain why GL balance ≠ bank balance.
        
        Args:
            school_id: School identifier
            reconciliation_id: BankReconciliation ID
            
        Returns:
            Summary of reconciling items
        """
        try:
            # Get unmatched items joined to their bank statement line, so we
            # can tell a bank fee/interest item apart from an ordinary
            # outstanding check or deposit in transit (transaction_type only
            # lives on BankStatement, not on the match record itself).
            unmatched_result = await self.session.execute(
                select(BankReconciliationMatch, BankStatement)
                .join(BankStatement, BankReconciliationMatch.bank_statement_id == BankStatement.id)
                .where(
                    and_(
                        BankReconciliationMatch.bank_reconciliation_id == reconciliation_id,
                        BankReconciliationMatch.school_id == school_id,
                        BankReconciliationMatch.match_status.in_([
                            BankItemStatus.UNMATCHED_BANK,
                            BankItemStatus.UNMATCHED_GL,
                        ])
                    )
                )
            )
            rows = unmatched_result.all()

            outstanding_checks = 0.0
            deposits_in_transit = 0.0
            bank_fees = 0.0

            for match, statement in rows:
                if statement.transaction_type in (BankTransactionType.FEE, BankTransactionType.INTEREST):
                    bank_fees += abs(match.bank_amount)
                elif match.bank_amount < 0:  # Withdrawal
                    outstanding_checks += abs(match.bank_amount)
                elif match.bank_amount > 0:  # Deposit
                    deposits_in_transit += match.bank_amount

            return {
                "reconciliation_id": reconciliation_id,
                "outstanding_checks": outstanding_checks,
                "deposits_in_transit": deposits_in_transit,
                "bank_fees": bank_fees,
                "total_reconciling_items": outstanding_checks + deposits_in_transit + bank_fees,
            }

        except Exception as e:
            logger.error(f"Error calculating reconciling items: {str(e)}")
            raise BankReconciliationError(f"Failed to calculate reconciling items: {str(e)}")

    async def create_reconciliation_adjustments(
        self,
        school_id: str,
        reconciliation_id: str,
        created_by: str,
    ) -> List[Dict[str, Any]]:
        """Create BankReconciliationAdjustment records for unmatched bank-fee
        and interest items on a reconciliation.

        This is the second half of the reconciliation workflow that
        calculate_reconciling_items only ever summarized (bank_fees was
        computed but no BankReconciliationAdjustment row was ever persisted).
        Adjustments are created unposted (is_posted=False) — posting them to
        GL requires choosing the offsetting expense/income account, which is
        a separate, explicit step (a finance user reviews and posts each
        adjustment rather than it happening silently here).

        Safe to call more than once on the same reconciliation: an item that
        already has a matching adjustment (by description + amount) is
        skipped, so repeated calls don't create duplicates.

        Args:
            school_id: School identifier
            reconciliation_id: BankReconciliation ID
            created_by: User creating the adjustments

        Returns:
            List of newly created adjustments (empty if nothing new to adjust)
        """
        try:
            existing_result = await self.session.execute(
                select(BankReconciliationAdjustment).where(
                    BankReconciliationAdjustment.bank_reconciliation_id == reconciliation_id,
                    BankReconciliationAdjustment.school_id == school_id,
                )
            )
            existing_keys = {
                (a.description, round(a.amount, 2)) for a in existing_result.scalars().all()
            }

            rows_result = await self.session.execute(
                select(BankReconciliationMatch, BankStatement)
                .join(BankStatement, BankReconciliationMatch.bank_statement_id == BankStatement.id)
                .where(
                    and_(
                        BankReconciliationMatch.bank_reconciliation_id == reconciliation_id,
                        BankReconciliationMatch.school_id == school_id,
                        BankReconciliationMatch.match_status.in_([
                            BankItemStatus.UNMATCHED_BANK,
                            BankItemStatus.UNMATCHED_GL,
                        ]),
                        BankStatement.transaction_type.in_(
                            [BankTransactionType.FEE, BankTransactionType.INTEREST]
                        ),
                    )
                )
            )

            created: List[BankReconciliationAdjustment] = []
            for match, statement in rows_result.all():
                key = (statement.description, round(match.bank_amount, 2))
                if key in existing_keys:
                    continue

                adjustment = BankReconciliationAdjustment(
                    school_id=school_id,
                    bank_reconciliation_id=reconciliation_id,
                    description=statement.description,
                    amount=match.bank_amount,
                    adjustment_type=(
                        "INTEREST" if statement.transaction_type == BankTransactionType.INTEREST else "BANK_FEE"
                    ),
                    created_by=created_by,
                )
                self.session.add(adjustment)
                created.append(adjustment)
                existing_keys.add(key)

            await self.session.commit()
            for adjustment in created:
                await self.session.refresh(adjustment)

            logger.info(
                f"Created {len(created)} reconciliation adjustments for {reconciliation_id}"
            )

            return [
                {
                    "id": a.id,
                    "description": a.description,
                    "amount": a.amount,
                    "adjustment_type": a.adjustment_type,
                    "is_posted": a.is_posted,
                }
                for a in created
            ]
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error creating reconciliation adjustments: {str(e)}")
            raise BankReconciliationError(f"Failed to create reconciliation adjustments: {str(e)}")
    
    async def _account_balance_as_of(
        self, school_id: str, gl_account: GLAccount, as_of_date: datetime,
    ) -> float:
        """GL account balance as of a specific date, from posted journal-entry
        line items — not gl_account.current_balance (the live running
        balance). Mirrors services/reports_service.py::_account_movements'
        posting_status/date filtering: POSTED and REVERSED entries both
        count (a reversed entry was posted, and its reversal is a separate
        posted contra-entry — both legs must be included for them to net to
        zero together), DRAFT never does.
        """
        cutoff = as_of_date.replace(tzinfo=None) if as_of_date.tzinfo else as_of_date
        result = await self.session.execute(
            select(
                func.coalesce(func.sum(JournalLineItem.debit_amount), 0),
                func.coalesce(func.sum(JournalLineItem.credit_amount), 0),
            ).select_from(JournalLineItem).join(
                JournalEntry, JournalEntry.id == JournalLineItem.journal_entry_id
            ).where(
                JournalLineItem.school_id == school_id,
                JournalLineItem.gl_account_id == gl_account.id,
                JournalEntry.posting_status.in_([PostingStatus.POSTED, PostingStatus.REVERSED]),
                JournalEntry.entry_date <= cutoff,
            )
        )
        debit, credit = result.one()
        debit, credit = float(debit), float(credit)
        return (credit - debit) if gl_account.normal_balance == "credit" else (debit - credit)

    async def calculate_variance(
        self,
        school_id: str,
        reconciliation_id: str,
    ) -> Dict[str, Any]:
        """Calculate variance between GL balance and bank balance
        
        Args:
            school_id: School identifier
            reconciliation_id: BankReconciliation ID
            
        Returns:
            Variance analysis
        """
        try:
            recon = await self.session.execute(
                select(BankReconciliation).where(
                    BankReconciliation.id == reconciliation_id,
                    BankReconciliation.school_id == school_id,
                )
            )
            reconciliation = recon.scalar_one_or_none()
            if not reconciliation:
                raise BankReconciliationError(f"Reconciliation {reconciliation_id} not found")

            # Get GL account
            gl_account = await self.coa_service.get_account_by_id(
                school_id,
                reconciliation.gl_account_id
            )
            if not gl_account:
                raise BankReconciliationError("GL account not found")

            # Balance AS OF the statement date, not gl_account.current_balance
            # (the account's live running balance right now). Any transaction
            # posted to this account after the statement date but before this
            # reconciliation is reviewed would otherwise show up as a false
            # variance against a bank balance that was never meant to include
            # it — same as/from services/reports_service.py::_account_movements,
            # which already gets this right for the trial balance/balance sheet.
            gl_balance = await self._account_balance_as_of(
                school_id, gl_account, reconciliation.statement_date
            )
            bank_balance = reconciliation.statement_ending_balance

            variance = bank_balance - gl_balance
            
            reconciliation.variance_amount = variance
            if abs(variance) < 0.01:
                reconciliation.variance_reason = "Reconciled"
            else:
                reconciliation.variance_reason = f"Variance of {variance:.2f} to review"
            
            self.session.add(reconciliation)
            await self.session.commit()
            
            return {
                "reconciliation_id": reconciliation_id,
                "bank_balance": bank_balance,
                "gl_balance": gl_balance,
                "variance": variance,
                "is_balanced": abs(variance) < 0.01,
            }
            
        except BankReconciliationError:
            raise
        except Exception as e:
            logger.error(f"Error calculating variance: {str(e)}")
            raise BankReconciliationError(f"Failed to calculate variance: {str(e)}")
    
    # ==================== Reconciliation Completion ====================
    
    async def complete_reconciliation(
        self,
        school_id: str,
        reconciliation_id: str,
        approved_by: str,
    ) -> Dict[str, Any]:
        """Mark reconciliation as completed
        
        Can only complete if variance is zero (or very close due to rounding).
        
        Args:
            school_id: School identifier
            reconciliation_id: BankReconciliation ID
            approved_by: User approving reconciliation
            
        Returns:
            Completion summary
            
        Raises:
            BankReconciliationError: If reconciliation cannot be completed
        """
        try:
            recon = await self.session.execute(
                select(BankReconciliation).where(
                    BankReconciliation.id == reconciliation_id,
                    BankReconciliation.school_id == school_id,
                )
            )
            reconciliation = recon.scalar_one_or_none()
            if not reconciliation:
                raise BankReconciliationError(f"Reconciliation {reconciliation_id} not found")

            # Check if balanced
            variance_result = await self.calculate_variance(school_id, reconciliation_id)
            if not variance_result["is_balanced"]:
                raise BankReconciliationError(
                    f"Cannot complete reconciliation with variance of {variance_result['variance']:.2f}"
                )
            
            # Mark as completed
            reconciliation.reconciliation_status = BankReconciliationStatus.COMPLETED
            reconciliation.approved_by = approved_by
            reconciliation.approved_date = datetime.utcnow()
            self.session.add(reconciliation)
            await self.session.commit()
            
            logger.info(
                f"Completed bank reconciliation {reconciliation_id} "
                f"for account {reconciliation.gl_account_id}"
            )
            
            return {
                "status": "success",
                "reconciliation_id": reconciliation_id,
                "completed_date": reconciliation.approved_date.isoformat(),
                "approved_by": approved_by,
            }
            
        except BankReconciliationError:
            raise
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error completing reconciliation: {str(e)}")
            raise BankReconciliationError(f"Failed to complete reconciliation: {str(e)}")
    
    # ==================== Reporting ====================
    
    async def get_reconciliation_summary(
        self,
        school_id: str,
        reconciliation_id: str,
    ) -> Dict[str, Any]:
        """Get reconciliation summary report
        
        Args:
            school_id: School identifier
            reconciliation_id: BankReconciliation ID
            
        Returns:
            Reconciliation summary with all key metrics
        """
        try:
            recon = await self.session.execute(
                select(BankReconciliation).where(
                    BankReconciliation.id == reconciliation_id,
                    BankReconciliation.school_id == school_id,
                )
            )
            reconciliation = recon.scalar_one_or_none()
            if not reconciliation:
                raise BankReconciliationError(f"Reconciliation {reconciliation_id} not found")

            variance = await self.calculate_variance(school_id, reconciliation_id)
            reconciling_items = await self.calculate_reconciling_items(school_id, reconciliation_id)
            
            return {
                "reconciliation_id": reconciliation_id,
                "gl_account_id": reconciliation.gl_account_id,
                "statement_date": reconciliation.statement_date.isoformat(),
                "bank_balance": reconciliation.statement_ending_balance,
                "gl_balance": variance["gl_balance"],
                "variance": variance["variance"],
                "is_balanced": variance["is_balanced"],
                "matched_transactions": reconciliation.matched_transactions,
                "unmatched_bank_items": reconciliation.unmatched_bank_items,
                "unmatched_gl_items": reconciliation.unmatched_gl_items,
                "status": reconciliation.reconciliation_status.value,
                "reconciled_by": reconciliation.reconciled_by,
                "approved_by": reconciliation.approved_by,
                "reconciling_items": reconciling_items,
            }
            
        except BankReconciliationError:
            raise
        except Exception as e:
            logger.error(f"Error getting reconciliation summary: {str(e)}")
            return {"error": str(e)}
