"""Journal Entry Service - Double-entry bookkeeping posting engine

Implements core accounting operations:
- Creating and validating journal entries
- Posting entries to general ledger
- Reversing (correcting) entries
- Trial balance calculations
- Entry status transitions
- GL account balance updates
"""
import logging
from typing import Optional, List, Dict, Any, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, and_, func
from datetime import datetime
from decimal import Decimal

from models.finance import (
    JournalEntry,
    JournalLineItem,
    JournalEntryCreate,
    JournalLineItemCreate,
    PostingStatus,
    ReferenceType,
)
from models.finance.chart_of_accounts import GLAccount
from models.school import School
from services.coa_service import CoaService
from services.fiscal_period_service import FiscalPeriodService

logger = logging.getLogger(__name__)


class JournalEntryError(Exception):
    """Base exception for journal entry service errors"""
    pass


class JournalEntryValidationError(JournalEntryError):
    """Raised when journal entry validation fails"""
    pass


class JournalEntryService:
    """Service for managing journal entries and GL postings
    
    Maintains the accounting equation:
      Debits = Credits at all times
    
    All posted entries are immutable - corrections require reversals with
    contra-entries to maintain audit trail.
    """
    
    def __init__(self, session: AsyncSession):
        """Initialize service with database session
        
        Args:
            session: AsyncSession for database operations
        """
        self.session = session
        self.coa_service = CoaService(session)
        self.fiscal_period_service = FiscalPeriodService(session)

    async def requires_maker_checker(self, school_id: str) -> bool:
        """Whether this school has segregation-of-duties enabled

        Off by default (School.require_maker_checker) — small schools with a
        single finance staffer can't otherwise use the approval workflow.
        """
        result = await self.session.execute(
            select(School.require_maker_checker).where(School.id == school_id)
        )
        return bool(result.scalar_one_or_none())
    
    # ==================== Entry Creation ====================
    
    async def create_entry(
        self,
        school_id: str,
        entry_data: JournalEntryCreate,
        created_by: str,
    ) -> JournalEntry:
        """Create a new journal entry in DRAFT status
        
        Validates:
        - At least 2 line items (1 debit, 1 credit minimum)
        - Total debits = Total credits
        - All GL accounts exist and are active
        - Amounts are positive
        
        Args:
            school_id: School identifier
            entry_data: Entry creation data with line items
            created_by: User creating the entry
            
        Returns:
            Created JournalEntry in DRAFT status
            
        Raises:
            JournalEntryValidationError: If validation fails
        """
        # Validate entry data
        validation_result = await self._validate_entry_data(
            school_id=school_id,
            entry_data=entry_data
        )
        if not validation_result["valid"]:
            raise JournalEntryValidationError(validation_result["error"])
        
        # Calculate totals
        total_debit = sum(item.debit_amount for item in entry_data.line_items)
        total_credit = sum(item.credit_amount for item in entry_data.line_items)

        # Resolve which fiscal period this entry falls into (by entry_date) so
        # post_entry() can enforce the period's lock/close status later. A None
        # here just means no period is configured for that date — posting is
        # then unrestricted, matching today's behavior.
        period = await self.fiscal_period_service.get_period_by_date(school_id, entry_data.entry_date)
        fiscal_period_id = period.id if period else None

        # Create entry
        entry = JournalEntry(
            school_id=school_id,
            entry_date=entry_data.entry_date,
            reference_type=entry_data.reference_type,
            reference_id=entry_data.reference_id,
            description=entry_data.description,
            total_debit=total_debit,
            total_credit=total_credit,
            posting_status=PostingStatus.DRAFT,
            fiscal_period_id=fiscal_period_id,
            is_adjusting_entry=entry_data.is_adjusting_entry,
            auto_reverse_date=entry_data.auto_reverse_date,
            created_by=created_by,
            notes=entry_data.notes,
        )
        
        self.session.add(entry)
        await self.session.flush()  # Get entry ID without commit
        
        # Create line items
        for idx, line_data in enumerate(entry_data.line_items):
            line_item = JournalLineItem(
                journal_entry_id=entry.id,
                school_id=school_id,
                gl_account_id=line_data.gl_account_id,
                debit_amount=line_data.debit_amount,
                credit_amount=line_data.credit_amount,
                description=line_data.description,
                line_number=line_data.line_number or idx + 1,
            )
            self.session.add(line_item)
        
        await self.session.commit()
        await self.session.refresh(entry)
        
        logger.info(
            f"Created journal entry {entry.id} ({entry.reference_type.value}) "
            f"for school {school_id} with {len(entry_data.line_items)} line items"
        )
        
        return entry
    
    # ==================== Validation ====================
    
    async def _validate_entry_data(
        self,
        school_id: str,
        entry_data: JournalEntryCreate
    ) -> Dict[str, Any]:
        """Validate journal entry data before creation
        
        Args:
            school_id: School identifier
            entry_data: Entry data to validate
            
        Returns:
            Dict with valid (bool) and error (str) if invalid
        """
        # Check minimum line items (1 debit, 1 credit)
        if len(entry_data.line_items) < 2:
            return {
                "valid": False,
                "error": "Entry must have at least 2 line items (1 debit, 1 credit)"
            }
        
        # Validate line items
        has_debit = False
        has_credit = False
        total_debit = Decimal("0")
        total_credit = Decimal("0")
        accounts_to_check = set()

        for line in entry_data.line_items:
            # Check that exactly one of debit/credit is > 0
            if line.debit_amount > 0 and line.credit_amount > 0:
                return {
                    "valid": False,
                    "error": f"Line item cannot have both debit and credit amounts"
                }
            
            if line.debit_amount == 0 and line.credit_amount == 0:
                return {
                    "valid": False,
                    "error": f"Line item must have either debit or credit amount"
                }
            
            if line.debit_amount > 0:
                has_debit = True
                total_debit += line.debit_amount
            
            if line.credit_amount > 0:
                has_credit = True
                total_credit += line.credit_amount
            
            accounts_to_check.add(line.gl_account_id)
        
        # Check for both debits and credits
        if not (has_debit and has_credit):
            return {
                "valid": False,
                "error": "Entry must have at least one debit and one credit line item"
            }
        
        # Check debits = credits (allow for rounding to 2 decimal places)
        if abs(total_debit - total_credit) > Decimal("0.01"):
            return {
                "valid": False,
                "error": f"Debits ({total_debit:.2f}) do not equal credits ({total_credit:.2f})"
            }
        
        # Validate GL accounts exist and are active
        for account_id in accounts_to_check:
            account = await self.coa_service.get_account_by_id(school_id, account_id)
            if not account:
                return {
                    "valid": False,
                    "error": f"GL Account {account_id} not found for school"
                }
            if not account.is_active:
                return {
                    "valid": False,
                    "error": f"GL Account {account_id} ({account.account_code}) is inactive"
                }
        
        return {"valid": True}
    
    # ==================== Entry Retrieval ====================
    
    async def get_entry_by_id(
        self,
        school_id: str,
        entry_id: str,
        include_line_items: bool = True
    ) -> Optional[Dict[str, Any]]:
        """Get a journal entry by ID with optional line items
        
        Args:
            school_id: School identifier
            entry_id: Entry ID to retrieve
            include_line_items: Include line items in response
            
        Returns:
            Entry data as dict, or None if not found
        """
        try:
            result = await self.session.execute(
                select(JournalEntry).where(
                    and_(
                        JournalEntry.school_id == school_id,
                        JournalEntry.id == entry_id
                    )
                )
            )
            entry = result.scalar_one_or_none()
            
            if not entry:
                return None
            
            return await self._entry_to_dict(entry, include_line_items)
            
        except Exception as e:
            logger.error(f"Error fetching entry {entry_id}: {str(e)}")
            return None
    
    async def get_entries_by_reference(
        self,
        school_id: str,
        reference_type: ReferenceType,
        reference_id: str,
    ) -> List[Dict[str, Any]]:
        """Get all entries for a specific reference (e.g., payroll run)
        
        Args:
            school_id: School identifier
            reference_type: Type of reference
            reference_id: Reference ID
            
        Returns:
            List of matching entries
        """
        try:
            result = await self.session.execute(
                select(JournalEntry).where(
                    and_(
                        JournalEntry.school_id == school_id,
                        JournalEntry.reference_type == reference_type,
                        JournalEntry.reference_id == reference_id
                    )
                ).order_by(JournalEntry.entry_date.desc())
            )
            entries = result.scalars().all()

            # Same full shape as get_entry_by_id/get_entries_filtered — this
            # previously returned a partial dict missing school_id,
            # reference_type, created_by, etc., which JournalEntryResponse
            # requires, so the /reference/{type}/{id} endpoint 500'd
            # whenever a match existed.
            return [await self._entry_to_dict(e, include_line_items=False) for e in entries]
        except Exception as e:
            logger.error(f"Error fetching entries for reference {reference_id}: {str(e)}")
            return []
    
    # ==================== Entry Posting ====================
    
    async def post_entry(
        self,
        school_id: str,
        entry_id: str,
        posted_by: str,
        approval_notes: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_role: str = "finance",
    ) -> Optional[JournalEntry]:
        """Post a DRAFT entry to GL (changes accounts to POSTED status)
        
        **CRITICAL OPERATION** - This updates GL account balances. Posts all line items to their 
        respective GL accounts by:
        - Increasing debit accounts for debit amounts
        - Increasing credit accounts for credit amounts
        - Updating GL account current_balance denormalized field
        
        Args:
            school_id: School identifier
            entry_id: Entry ID to post
            posted_by: User posting the entry
            approval_notes: Optional approval notes
            ip_address: IP address of user (for audit trail)
            user_role: Role of user posting (for audit trail)
            
        Returns:
            Posted JournalEntry, or None if not found or cannot be posted
            
        Raises:
            JournalEntryError: If entry cannot be posted
        """
        # Get entry
        result = await self.session.execute(
            select(JournalEntry).where(
                and_(
                    JournalEntry.school_id == school_id,
                    JournalEntry.id == entry_id
                )
            )
        )
        entry = result.scalar_one_or_none()
        
        if not entry:
            raise JournalEntryError(f"Entry {entry_id} not found")
        
        if entry.posting_status != PostingStatus.DRAFT:
            raise JournalEntryError(
                f"Cannot post entry with status {entry.posting_status.value} "
                f"(only DRAFT entries can be posted)"
            )
        
        # Get line items
        result = await self.session.execute(
            select(JournalLineItem).where(
                JournalLineItem.journal_entry_id == entry_id
            )
        )
        line_items = result.scalars().all()
        
        if not line_items:
            raise JournalEntryError(f"Entry {entry_id} has no line items")
        
        # Check if entry is adjusting
        is_adjusting = getattr(entry, 'is_adjusting_entry', False)

        # Enforce fiscal period locking: a LOCKED/CLOSED period rejects new
        # postings (adjusting entries may still be allowed into a LOCKED
        # period if the period permits it — see can_post_to_period).
        if entry.fiscal_period_id:
            can_post, reason = await self.fiscal_period_service.can_post_to_period(
                school_id, entry.fiscal_period_id, is_adjustment_entry=is_adjusting,
            )
            if not can_post:
                raise JournalEntryError(f"Cannot post entry: {reason}")

        # ⭐ CRITICAL: Update GL account balances and flip entry status as a
        # single transaction — either all of it lands, or none of it does.
        # (update_account_balance(commit=False) stages the change but leaves
        # committing to us, so a failure partway through the loop rolls back
        # every balance change already staged, instead of leaving the ledger
        # half-posted.)
        try:
            for line_item in line_items:
                updated = await self.coa_service.update_account_balance(
                    school_id=school_id,
                    account_id=line_item.gl_account_id,
                    debit_amount=line_item.debit_amount,
                    credit_amount=line_item.credit_amount,
                    commit=False,
                )
                if updated is None:
                    raise JournalEntryError(
                        f"GL account {line_item.gl_account_id} not found while posting"
                    )

            entry.posting_status = PostingStatus.POSTED
            entry.posted_date = datetime.utcnow()
            entry.posted_by = posted_by
            entry.posted_ip = ip_address
            entry.notes = approval_notes or entry.notes
            entry.updated_at = datetime.utcnow()

            self.session.add(entry)
            await self.session.commit()
            await self.session.refresh(entry)
        except JournalEntryError:
            await self.session.rollback()
            raise
        except Exception as e:
            await self.session.rollback()
            raise JournalEntryError(f"Failed to update GL account balances: {str(e)}")
        
        logger.info(
            f"Posted journal entry {entry_id} ({entry.reference_type.value}) "
            f"for school {school_id} with {len(line_items)} line items "
            f"(Total: DR {entry.total_debit} CR {entry.total_credit})"
        )
        
        return entry
    
    # ==================== Entry Reversal ====================
    
    async def assert_reversal_allowed(self, school_id: str, original_entry: JournalEntry, reversed_by: str) -> None:
        """Segregation of duties for reversals — previously NO reversal
        path checked this at all, unlike normal manual-entry posting
        (routers/finance/journal.py blocks a creator from also posting
        their own entry). Without this, a single user with reversal
        permission could unilaterally create AND post a real, GL-impacting
        correcting entry against their own work, even at a school with
        segregation-of-duties turned on — exactly the scenario that
        control exists to catch. Never blocks reversing a SYSTEM-generated
        original entry (fee payments, depreciation, etc.) — reversing an
        automated posting isn't a human "cheating" the control; it's
        exactly what void_payment and similar flows are supposed to do."""
        if original_entry.created_by == "SYSTEM" or original_entry.posted_by == "SYSTEM":
            return
        result = await self.session.execute(select(School.require_maker_checker).where(School.id == school_id))
        if not bool(result.scalar_one_or_none()):
            return
        if original_entry.created_by == reversed_by or original_entry.posted_by == reversed_by:
            raise JournalEntryError(
                "Segregation of duties: you created or posted this entry and cannot also reverse it — "
                "ask another finance user to reverse it"
            )

    async def reverse_entry(
        self,
        school_id: str,
        entry_id: str,
        reversed_by: str,
        reversal_reason: str,
        reversal_notes: Optional[str] = None,
    ) -> Tuple[Optional[JournalEntry], Optional[JournalEntry]]:
        """Reverse (correct) a posted journal entry
        
        Creates a contra-entry that exactly reverses the original entry,
        maintaining audit trail.
        
        Returns:
            Tuple of (original_entry, reversal_entry)
            
        Raises:
            JournalEntryError: If entry cannot be reversed
        """
        # Get original entry
        result = await self.session.execute(
            select(JournalEntry).where(
                and_(
                    JournalEntry.school_id == school_id,
                    JournalEntry.id == entry_id
                )
            )
        )
        original_entry = result.scalar_one_or_none()
        
        if not original_entry:
            raise JournalEntryError(f"Entry {entry_id} not found")
        
        if original_entry.posting_status != PostingStatus.POSTED:
            raise JournalEntryError(
                f"Can only reverse POSTED entries "
                f"(current status: {original_entry.posting_status.value})"
            )

        await self.assert_reversal_allowed(school_id, original_entry, reversed_by)

        # Get line items
        result = await self.session.execute(
            select(JournalLineItem).where(
                JournalLineItem.journal_entry_id == entry_id
            )
        )
        line_items = result.scalars().all()
        
        # Create reversal entry with opposite amounts
        reversal_line_items = []
        for li in line_items:
            reversal_line_items.append(
                JournalLineItemCreate(
                    gl_account_id=li.gl_account_id,
                    debit_amount=li.credit_amount,  # Swap debit/credit
                    credit_amount=li.debit_amount,
                    description=f"Reversal: {li.description}" if li.description else "Reversal",
                    line_number=li.line_number,
                )
            )
        
        # Create reversal entry. Dated "now" (not the original entry's date),
        # so if the *current* period has been LOCKED for audit, this would
        # otherwise be blocked from posting at all — a reversal is exactly
        # the kind of corrective action a LOCKED (not CLOSED) period is
        # still supposed to allow, so it's marked as an adjusting entry the
        # same way RetainedEarningsService's closing entry is.
        reversal_entry_data = JournalEntryCreate(
            entry_date=datetime.utcnow(),
            reference_type=ReferenceType.ADJUSTMENT,
            reference_id=entry_id,  # Link to original
            description=f"Reversal of {original_entry.reference_type.value} - {reversal_reason}",
            line_items=reversal_line_items,
            notes=reversal_notes or "",
            is_adjusting_entry=True,
        )
        
        # Create the reversal entry
        try:
            reversal_entry = await self.create_entry(
                school_id=school_id,
                entry_data=reversal_entry_data,
                created_by=reversed_by,
            )
            
            # ⭐ CRITICAL: Post the reversal immediately (updates balances & logs)
            await self.post_entry(
                school_id=school_id,
                entry_id=reversal_entry.id,
                posted_by=reversed_by,
                approval_notes=f"Auto-posted reversal of {entry_id}",
            )
            
            # Update original entry to mark as reversed
            original_entry.posting_status = PostingStatus.REVERSED
            original_entry.reversal_entry_id = reversal_entry.id
            original_entry.reversed_date = datetime.utcnow()
            original_entry.reversed_by = reversed_by
            original_entry.updated_at = datetime.utcnow()
            
            self.session.add(original_entry)
            await self.session.commit()
            
            logger.info(
                f"Reversed journal entry {entry_id} with reversal entry {reversal_entry.id} "
                f"(Reason: {reversal_reason})"
            )
            
            return (original_entry, reversal_entry)
            
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error reversing entry {entry_id}: {str(e)}")
            raise JournalEntryError(f"Failed to reverse entry: {str(e)}")
    
    # ==================== Analysis & Reporting ====================
    
    async def get_trial_balance(
        self,
        school_id: str,
        as_of_date: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Generate trial balance (total debits and credits by account)
        
        Trial balance verifies that total debits = total credits.
        Should be run after period close.
        
        Args:
            school_id: School identifier
            as_of_date: Optional cutoff date (default: all posted entries)
            
        Returns:
            Dict with total_debit, total_credit, by_account details
        """
        try:
            # Line items from DRAFT entries never touched the GL and must be
            # excluded, or this trial balance would disagree with the
            # canonical one in services/reports_service.py (which already
            # gets this right). REVERSED is included alongside POSTED: a
            # reversed entry *was* posted, and its reversal is a separate,
            # also-posted contra-entry -- both legs must be included for them
            # to net to zero together.
            conditions = [
                JournalLineItem.school_id == school_id,
                JournalEntry.id == JournalLineItem.journal_entry_id,
                JournalEntry.posting_status.in_([PostingStatus.POSTED, PostingStatus.REVERSED]),
            ]
            if as_of_date is not None:
                cutoff = as_of_date.replace(tzinfo=None) if as_of_date.tzinfo else as_of_date
                conditions.append(JournalEntry.entry_date <= cutoff)
            query = select(JournalLineItem).where(and_(*conditions))

            result = await self.session.execute(query)
            line_items = result.scalars().all()
            
            # Calculate by account
            by_account = {}
            total_debit = Decimal("0")
            total_credit = Decimal("0")

            for li in line_items:
                if li.gl_account_id not in by_account:
                    by_account[li.gl_account_id] = {
                        "debit": Decimal("0"),
                        "credit": Decimal("0"),
                    }

                by_account[li.gl_account_id]["debit"] += li.debit_amount
                by_account[li.gl_account_id]["credit"] += li.credit_amount

                total_debit += li.debit_amount
                total_credit += li.credit_amount

            return {
                "total_debit": total_debit,
                "total_credit": total_credit,
                "balanced": abs(total_debit - total_credit) < Decimal("0.01"),
                "by_account": by_account,
            }
            
        except Exception as e:
            logger.error(f"Error generating trial balance for school {school_id}: {str(e)}")
            return {
                "error": str(e),
                "balanced": False,
            }
    
    async def get_entry_summary(
        self,
        school_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Get summary of journal entries for a period
        
        Args:
            school_id: School identifier
            start_date: Optional start date (default: all)
            end_date: Optional end date (default: all)
            
        Returns:
            Summary dict with counts and amounts by status
        """
        try:
            query = select(JournalEntry).where(
                JournalEntry.school_id == school_id
            )
            
            if start_date:
                query = query.where(JournalEntry.entry_date >= start_date)
            if end_date:
                query = query.where(JournalEntry.entry_date <= end_date)
            
            result = await self.session.execute(query)
            entries = result.scalars().all()
            
            summary = {
                "total_entries": len(entries),
                "posted": 0,
                "draft": 0,
                "reversed": 0,
                "rejected": 0,
                "total_posted_amount": Decimal("0"),
            }
            
            for entry in entries:
                if entry.posting_status == PostingStatus.POSTED:
                    summary["posted"] += 1
                    summary["total_posted_amount"] += entry.total_debit
                elif entry.posting_status == PostingStatus.DRAFT:
                    summary["draft"] += 1
                elif entry.posting_status == PostingStatus.REVERSED:
                    summary["reversed"] += 1
                elif entry.posting_status == PostingStatus.REJECTED:
                    summary["rejected"] += 1
            
            return summary
            
        except Exception as e:
            logger.error(f"Error generating entry summary for school {school_id}: {str(e)}")
            return {"error": str(e)}

    async def get_entries_filtered(
        self,
        school_id: str,
        posting_status: Optional[PostingStatus] = None,
        reference_type: Optional[ReferenceType] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        skip: int = 0,
        limit: int = 50,
        include_line_items: bool = True,
    ) -> List[Dict[str, Any]]:
        """Get filtered list of journal entries with pagination
        
        Args:
            school_id: School identifier
            posting_status: Optional filter by status (draft, posted, reversed, rejected)
            reference_type: Optional filter by reference type
            start_date: Optional start date filter
            end_date: Optional end date filter
            skip: Number of entries to skip (pagination offset)
            limit: Maximum entries to return (pagination limit)
            include_line_items: Whether to fetch and include line items
            
        Returns:
            List of entry dictionaries, ordered by entry_date descending
        """
        try:
            query = select(JournalEntry).where(
                JournalEntry.school_id == school_id
            )
            
            if posting_status:
                query = query.where(JournalEntry.posting_status == posting_status)
            if reference_type:
                query = query.where(JournalEntry.reference_type == reference_type)
            if start_date:
                query = query.where(JournalEntry.entry_date >= start_date)
            if end_date:
                query = query.where(JournalEntry.entry_date <= end_date)
            
            # Order by entry_date descending (most recent first)
            query = query.order_by(JournalEntry.entry_date.desc())
            query = query.offset(skip).limit(limit)
            
            result = await self.session.execute(query)
            entries = result.scalars().all()
            
            return [await self._entry_to_dict(entry, include_line_items) for entry in entries]
            
        except Exception as e:
            logger.error(f"Error filtering entries for school {school_id}: {str(e)}")
            raise JournalEntryError(f"Error filtering entries: {str(e)}")

    async def update_entry(
        self,
        school_id: str,
        entry_id: str,
        update_data,
    ) -> Dict[str, Any]:
        """Update a draft journal entry
        
        Args:
            school_id: School identifier
            entry_id: Entry to update
            update_data: JournalEntryUpdate with new values
            
        Returns:
            Updated entry dictionary
            
        Raises:
            JournalEntryError: If entry not found or cannot be updated
            JournalEntryValidationError: If update violates double-entry rules
        """
        try:
            # Get the entry
            result = await self.session.execute(
                select(JournalEntry).where(
                    and_(
                        JournalEntry.id == entry_id,
                        JournalEntry.school_id == school_id,
                    )
                )
            )
            entry = result.scalar_one_or_none()
            
            if not entry:
                raise JournalEntryError(f"Entry {entry_id} not found")
            
            if entry.posting_status != PostingStatus.DRAFT:
                raise JournalEntryError(
                    f"Cannot update entry with status {entry.posting_status} "
                    "(only DRAFT entries can be updated)"
                )
            
            # Update simple fields
            if update_data.description:
                entry.description = update_data.description
            if update_data.notes:
                entry.notes = update_data.notes
            
            # If line items are provided, replace them all
            if update_data.line_items:
                # Delete existing line items
                await self.session.execute(
                    select(JournalLineItem).where(
                        JournalLineItem.journal_entry_id == entry_id
                    )
                )
                result = await self.session.execute(
                    select(JournalLineItem).where(
                        JournalLineItem.journal_entry_id == entry_id
                    )
                )
                for line in result.scalars().all():
                    await self.session.delete(line)
                
                # Create new line items with validation
                total_debit = Decimal("0")
                total_credit = Decimal("0")
                
                for line_num, line_data in enumerate(update_data.line_items, 1):
                    line_item = JournalLineItem(
                        journal_entry_id=entry_id,
                        school_id=school_id,
                        gl_account_id=line_data.gl_account_id,
                        debit_amount=line_data.debit_amount,
                        credit_amount=line_data.credit_amount,
                        description=line_data.description,
                        line_number=line_num,
                    )
                    self.session.add(line_item)
                    total_debit += line_data.debit_amount
                    total_credit += line_data.credit_amount
                
                # Re-validate the updated entry
                entry.total_debit = total_debit
                entry.total_credit = total_credit
                entry.updated_at = datetime.utcnow()
                
                # Validate updated data
                validation = await self._validate_entry_data(
                    school_id=school_id,
                    entry_data=update_data,
                )
                
                if not validation["valid"]:
                    raise JournalEntryValidationError(
                        f"Updated entry validation failed: {validation['error']}"
                    )
            
            await self.session.commit()
            
            return await self._entry_to_dict(entry, include_line_items=True)
            
        except (JournalEntryError, JournalEntryValidationError):
            await self.session.rollback()
            raise
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error updating entry {entry_id}: {str(e)}")
            raise JournalEntryError(f"Error updating entry: {str(e)}")

    # ==================== Entry Deletion ====================

    async def delete_entry(
        self,
        school_id: str,
        entry_id: str,
    ) -> bool:
        """Delete a draft journal entry
        
        Only DRAFT entries can be deleted. Posted entries must be reversed instead.
        
        Args:
            school_id: School identifier
            entry_id: Entry ID to delete
            
        Returns:
            True if deletion was successful
            
        Raises:
            JournalEntryError: If entry not found or cannot be deleted
        """
        try:
            # Get the entry
            result = await self.session.execute(
                select(JournalEntry).where(
                    and_(
                        JournalEntry.id == entry_id,
                        JournalEntry.school_id == school_id,
                    )
                )
            )
            entry = result.scalar_one_or_none()
            
            if not entry:
                raise JournalEntryError(f"Entry {entry_id} not found")
            
            if entry.posting_status != PostingStatus.DRAFT:
                raise JournalEntryError(
                    f"Cannot delete entry with status {entry.posting_status} "
                    "(only DRAFT entries can be deleted; use reversal for posted entries)"
                )
            
            # Delete line items first (due to foreign key constraint)
            result = await self.session.execute(
                select(JournalLineItem).where(
                    JournalLineItem.journal_entry_id == entry_id
                )
            )
            line_items = result.scalars().all()
            for line in line_items:
                await self.session.delete(line)
            
            # Delete entry
            await self.session.delete(entry)
            await self.session.commit()
            
            logger.info(f"Deleted journal entry {entry_id} for school {school_id}")
            return True
            
        except JournalEntryError:
            await self.session.rollback()
            raise
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error deleting entry {entry_id}: {str(e)}")
            raise JournalEntryError(f"Error deleting entry: {str(e)}")

    # ==================== Helper Methods ====================
    
    async def _entry_to_dict(
        self,
        entry: JournalEntry,
        include_line_items: bool = False,
    ) -> Dict[str, Any]:
        """Convert journal entry to dictionary for API responses
        
        Args:
            entry: JournalEntry object
            include_line_items: Whether to include line items in response
            
        Returns:
            Dictionary representation of the entry
        """
        entry_dict = {
            "id": entry.id,
            "school_id": entry.school_id,
            "description": entry.description,
            "notes": entry.notes,
            "entry_date": entry.entry_date.isoformat() if entry.entry_date else None,
            "posting_status": entry.posting_status.value if hasattr(entry.posting_status, 'value') else str(entry.posting_status),
            "reference_type": entry.reference_type.value if hasattr(entry.reference_type, 'value') else str(entry.reference_type) if entry.reference_type else None,
            "reference_id": entry.reference_id,
            "total_debit": float(entry.total_debit) if entry.total_debit else 0.0,
            "total_credit": float(entry.total_credit) if entry.total_credit else 0.0,
            "posted_date": entry.posted_date.isoformat() if entry.posted_date else None,
            "posted_by": entry.posted_by,
            "reversal_entry_id": entry.reversal_entry_id if hasattr(entry, 'reversal_entry_id') else None,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
            "created_by": entry.created_by,
            "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
        }
        
        if include_line_items:
            # Get line items from database
            result = await self.session.execute(
                select(JournalLineItem).where(
                    JournalLineItem.journal_entry_id == entry.id
                ).order_by(JournalLineItem.line_number)
            )
            line_items = result.scalars().all()
            
            entry_dict["line_items"] = [
                {
                    "id": item.id,
                    "journal_entry_id": item.journal_entry_id,
                    "gl_account_id": item.gl_account_id,
                    "debit_amount": float(item.debit_amount) if item.debit_amount else 0.0,
                    "credit_amount": float(item.credit_amount) if item.credit_amount else 0.0,
                    "description": item.description,
                    "line_number": item.line_number,
                    "created_at": item.created_at.isoformat() if item.created_at else None,
                }
                for item in line_items
            ]
        
        return entry_dict
            
         