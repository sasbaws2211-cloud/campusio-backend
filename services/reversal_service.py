"""Reversal Service - Advanced reversal entry management

Handles complex reversal scenarios:
- Full entry reversal (complete reversal with contra-entry)
- Partial reversal (specific line items only)
- Reversal patterns (for recurring corrections)
- Reversal reporting and audit trail
"""
import logging
from typing import Optional, List, Dict, Any, Tuple
from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, and_, func
from datetime import datetime
import uuid

from models.finance import (
    JournalEntry,
    JournalLineItem,
    JournalEntryCreate,
    JournalLineItemCreate,
    PostingStatus,
    ReferenceType,
)
from models.finance.gl_audit_log import AuditEntityType, AuditActionType
from services.journal_entry_service import JournalEntryService, JournalEntryError
from services.gl_audit_log_service import GLAuditLogService

logger = logging.getLogger(__name__)


class ReversalError(Exception):
    """Base exception for reversal operations"""
    pass


class ReversalService:
    """Service for managing advanced reversal scenarios
    
    Reversals are essential for:
    - Correcting erroneous entries
    - Undoing transactions
    - Maintaining audit trail (original + reversal visible)
    
    Approach:
    - Never delete posted entries
    - Create contra-entries that reverse the effect
    - Both original and reversal visible in audit trail
    - GL balances restore to pre-reversal state
    """
    
    def __init__(self, session: AsyncSession):
        """Initialize service with database session
        
        Args:
            session: AsyncSession for database operations
        """
        self.session = session
        self.journal_service = JournalEntryService(session)
        self.audit_service = GLAuditLogService(session)
    
    # ==================== Full Reversal ====================
    
    async def reverse_full_entry(
        self,
        school_id: str,
        entry_id: str,
        reversed_by: str,
        reversal_reason: str,
        reversal_notes: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_role: str = "finance",
        user_name: str = "Unknown",
    ) -> Tuple[JournalEntry, JournalEntry]:
        """Reverse an entire posted journal entry

        Delegates to JournalEntryService.reverse_entry — the same routine
        journal.py's own /reverse endpoint uses — instead of re-implementing
        full-entry reversal here. The two copies previously drifted: this one
        wrote to a `reversal_date` attribute that doesn't exist on the model
        (JournalEntry only has `reversed_date`), so every reversal made
        through this endpoint silently failed to persist that field.

        Args:
            school_id: School identifier
            entry_id: Entry ID to reverse
            reversed_by: User reversing
            reversal_reason: Reason for reversal
            reversal_notes: Optional additional notes
            ip_address: IP address for audit
            user_role: User role for audit
            user_name: Display name of the user, for the audit log

        Returns:
            Tuple of (original_entry, reversal_entry)

        Raises:
            ReversalError: If reversal cannot be completed
        """
        try:
            original_entry, reversal_entry = await self.journal_service.reverse_entry(
                school_id=school_id,
                entry_id=entry_id,
                reversed_by=reversed_by,
                reversal_reason=reversal_reason,
                reversal_notes=reversal_notes,
            )
        except JournalEntryError as e:
            raise ReversalError(str(e))
        except Exception as e:
            logger.error(f"Error reversing entry {entry_id}: {str(e)}")
            raise ReversalError(f"Failed to reverse entry: {str(e)}")

        try:
            await self.audit_service.log_action(
                school_id=school_id,
                entity_type=AuditEntityType.JOURNAL_ENTRY,
                entity_id=entry_id,
                action=AuditActionType.ENTRY_REVERSED,
                user_id=reversed_by,
                user_name=user_name,
                user_role=user_role,
                new_values={"reversal_reason": reversal_reason, "reversal_entry_id": reversal_entry.id},
                ip_address=ip_address,
            )
        except Exception as e:
            logger.warning(f"Failed to write GL audit log for reversal of {entry_id}: {e}")

        logger.info(
            f"Fully reversed entry {entry_id} with reversal {reversal_entry.id} "
            f"(reason: {reversal_reason}, user: {reversed_by})"
        )

        return (original_entry, reversal_entry)
    
    # ==================== Partial Reversal ====================
    
    async def reverse_partial_entry(
        self,
        school_id: str,
        entry_id: str,
        line_numbers: List[int],
        reversed_by: str,
        reversal_reason: str,
        reversal_notes: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_role: str = "finance",
        user_name: str = "Unknown",
    ) -> Tuple[JournalEntry, JournalEntry]:
        """Reverse specific line items from a posted entry
        
        Creates a contra-entry that reverses only selected line items.
        Useful for partial corrections.
        
        Args:
            school_id: School identifier
            entry_id: Entry ID to partially reverse
            line_numbers: List of line numbers to reverse
            reversed_by: User reversing
            reversal_reason: Reason for reversal
            reversal_notes: Optional notes
            ip_address: IP address for audit
            user_role: User role for audit
            
        Returns:
            Tuple of (original_entry, partial_reversal_entry)
            
        Raises:
            ReversalError: If reversal cannot be completed
        """
        try:
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
                raise ReversalError(f"Entry {entry_id} not found")
            
            if original_entry.posting_status != PostingStatus.POSTED:
                raise ReversalError(f"Can only partially reverse POSTED entries")

            try:
                await self.journal_service.assert_reversal_allowed(school_id, original_entry, reversed_by)
            except JournalEntryError as e:
                raise ReversalError(str(e))

            # Get line items
            result = await self.session.execute(
                select(JournalLineItem).where(
                    JournalLineItem.journal_entry_id == entry_id
                ).order_by(JournalLineItem.line_number)
            )
            all_line_items = result.scalars().all()
            
            # Validate line numbers exist
            valid_line_numbers = {li.line_number for li in all_line_items}
            for ln in line_numbers:
                if ln not in valid_line_numbers:
                    raise ReversalError(f"Line number {ln} not found in entry")
            
            # Build partial reversal (swap only selected line items)
            reversal_lines = []
            total_debit = Decimal("0")
            total_credit = Decimal("0")
            
            for li in all_line_items:
                if li.line_number in line_numbers:
                    # Reverse this line
                    reversal_lines.append(
                        JournalLineItemCreate(
                            gl_account_id=li.gl_account_id,
                            debit_amount=li.credit_amount,
                            credit_amount=li.debit_amount,
                            description=f"Partial Reversal: {li.description}" if li.description else "Partial Reversal",
                            line_number=li.line_number,
                        )
                    )
                    total_debit += li.credit_amount
                    total_credit += li.debit_amount
            
            # Validate partial reversal is balanced
            if abs(total_debit - total_credit) > Decimal("0.01"):
                raise ReversalError(
                    f"Selected line items do not balance. "
                    f"Debits: {total_debit:.2f}, Credits: {total_credit:.2f}"
                )
            
            # Create partial reversal entry
            reversal_entry_data = JournalEntryCreate(
                entry_date=datetime.utcnow(),
                reference_type=ReferenceType.ADJUSTMENT,
                reference_id=entry_id,
                description=f"Partial Reversal ({len(line_numbers)} lines) - {reversal_reason}",
                line_items=reversal_lines,
                notes=f"Lines reversed: {', '.join(map(str, line_numbers))}. {reversal_notes or ''}".strip(),
            )
            
            # Create and post reversal
            reversal_entry = await self.journal_service.create_entry(
                school_id=school_id,
                entry_data=reversal_entry_data,
                created_by=reversed_by,
            )
            
            await self.journal_service.post_entry(
                school_id=school_id,
                entry_id=reversal_entry.id,
                posted_by=reversed_by,
                approval_notes=f"Partial reversal of {entry_id}: {reversal_reason}",
                ip_address=ip_address,
                user_role=user_role,
            )

            try:
                await self.audit_service.log_action(
                    school_id=school_id,
                    entity_type=AuditEntityType.JOURNAL_ENTRY,
                    entity_id=entry_id,
                    action=AuditActionType.ENTRY_REVERSED,
                    user_id=reversed_by,
                    user_name=user_name,
                    user_role=user_role,
                    new_values={
                        "reversal_reason": reversal_reason,
                        "reversal_entry_id": reversal_entry.id,
                        "lines_reversed": line_numbers,
                    },
                    ip_address=ip_address,
                )
            except Exception as e:
                logger.warning(f"Failed to write GL audit log for partial reversal of {entry_id}: {e}")

            logger.info(
                f"Partially reversed entry {entry_id} (lines: {line_numbers}) "
                f"with reversal {reversal_entry.id} (reason: {reversal_reason})"
            )

            return (original_entry, reversal_entry)
            
        except ReversalError:
            await self.session.rollback()
            raise
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error partially reversing entry {entry_id}: {str(e)}")
            raise ReversalError(f"Failed to partially reverse entry: {str(e)}")
    
    # ==================== Reversal by Account ====================
    
    async def reverse_specific_accounts(
        self,
        school_id: str,
        entry_id: str,
        account_ids: List[str],
        reversed_by: str,
        reversal_reason: str,
        reversal_notes: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_role: str = "finance",
        user_name: str = "Unknown",
    ) -> Tuple[JournalEntry, JournalEntry]:
        """Reverse postings to specific GL accounts only
        
        Args:
            school_id: School identifier
            entry_id: Entry ID to partially reverse
            account_ids: List of GL account IDs to reverse
            reversed_by: User reversing
            reversal_reason: Reason for reversal
            reversal_notes: Optional notes
            ip_address: IP address for audit
            user_role: User role for audit
            
        Returns:
            Tuple of (original_entry, account_reversal_entry)
            
        Raises:
            ReversalError: If reversal cannot be completed
        """
        try:
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
                raise ReversalError(f"Entry {entry_id} not found")

            try:
                await self.journal_service.assert_reversal_allowed(school_id, original_entry, reversed_by)
            except JournalEntryError as e:
                raise ReversalError(str(e))

            # Get line items for specified accounts
            result = await self.session.execute(
                select(JournalLineItem).where(
                    and_(
                        JournalLineItem.journal_entry_id == entry_id,
                        JournalLineItem.gl_account_id.in_(account_ids)
                    )
                )
            )
            selected_items = result.scalars().all()
            
            if not selected_items:
                raise ReversalError(f"No line items found for specified accounts")
            
            # Build reversal
            reversal_lines = []
            for li in selected_items:
                reversal_lines.append(
                    JournalLineItemCreate(
                        gl_account_id=li.gl_account_id,
                        debit_amount=li.credit_amount,
                        credit_amount=li.debit_amount,
                        description=f"Reversal: {li.description}" if li.description else "Reversal",
                        line_number=li.line_number,
                    )
                )
            
            # Create reversal entry
            reversal_entry_data = JournalEntryCreate(
                entry_date=datetime.utcnow(),
                reference_type=ReferenceType.ADJUSTMENT,
                reference_id=entry_id,
                description=f"Account-Specific Reversal ({len(account_ids)} accounts) - {reversal_reason}",
                line_items=reversal_lines,
                notes=reversal_notes or "",
            )
            
            # Create and post reversal
            reversal_entry = await self.journal_service.create_entry(
                school_id=school_id,
                entry_data=reversal_entry_data,
                created_by=reversed_by,
            )
            
            await self.journal_service.post_entry(
                school_id=school_id,
                entry_id=reversal_entry.id,
                posted_by=reversed_by,
                approval_notes=f"Account-specific reversal of {entry_id}: {reversal_reason}",
                ip_address=ip_address,
                user_role=user_role,
            )

            try:
                await self.audit_service.log_action(
                    school_id=school_id,
                    entity_type=AuditEntityType.JOURNAL_ENTRY,
                    entity_id=entry_id,
                    action=AuditActionType.ENTRY_REVERSED,
                    user_id=reversed_by,
                    user_name=user_name,
                    user_role=user_role,
                    new_values={
                        "reversal_reason": reversal_reason,
                        "reversal_entry_id": reversal_entry.id,
                        "accounts_reversed": account_ids,
                    },
                    ip_address=ip_address,
                )
            except Exception as e:
                logger.warning(f"Failed to write GL audit log for account reversal of {entry_id}: {e}")

            logger.info(
                f"Reversed accounts {account_ids} in entry {entry_id} "
                f"with reversal {reversal_entry.id}"
            )

            return (original_entry, reversal_entry)
            
        except ReversalError:
            await self.session.rollback()
            raise
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error reversing specific accounts: {str(e)}")
            raise ReversalError(f"Failed to reverse specific accounts: {str(e)}")
    
    # ==================== Reversal Analysis ====================
    
    async def get_reversal_chain(
        self,
        school_id: str,
        entry_id: str,
    ) -> Dict[str, Any]:
        """Get complete reversal chain for an entry
        
        Shows: Original → Reversal → Re-reversal (if applicable)
        
        Args:
            school_id: School identifier
            entry_id: Entry ID to trace
            
        Returns:
            Dictionary with complete reversal chain
        """
        try:
            chain = []
            current_id = entry_id
            visited = set()
            
            while current_id and current_id not in visited:
                visited.add(current_id)
                
                result = await self.session.execute(
                    select(JournalEntry).where(
                        and_(
                            JournalEntry.school_id == school_id,
                            JournalEntry.id == current_id
                        )
                    )
                )
                entry = result.scalar_one_or_none()
                
                if not entry:
                    break
                
                chain.append({
                    "id": entry.id,
                    "entry_date": entry.entry_date,
                    "description": entry.description,
                    "total_debit": float(entry.total_debit),
                    "total_credit": float(entry.total_credit),
                    "posting_status": entry.posting_status.value,
                    "posted_by": entry.posted_by,
                    "posted_date": entry.posted_date,
                })
                
                # Follow reversal chain
                if entry.reversal_entry_id:
                    current_id = entry.reversal_entry_id
                else:
                    break
            
            return {
                "entry_id": entry_id,
                "chain_length": len(chain),
                "chain": chain,
            }
        except Exception as e:
            logger.error(f"Error getting reversal chain: {str(e)}")
            return {"error": str(e)}
    
    async def get_reversals_for_period(
        self,
        school_id: str,
        start_date: datetime,
        end_date: datetime,
    ) -> List[Dict[str, Any]]:
        """Get all reversals within a date range
        
        Useful for audit and analysis.
        
        Args:
            school_id: School identifier
            start_date: Start of date range
            end_date: End of date range
            
        Returns:
            List of reversal entries
        """
        try:
            result = await self.session.execute(
                select(JournalEntry).where(
                    and_(
                        JournalEntry.school_id == school_id,
                        JournalEntry.posting_status == PostingStatus.REVERSED,
                        JournalEntry.reversed_date >= start_date,
                        JournalEntry.reversed_date <= end_date,
                    )
                ).order_by(JournalEntry.reversed_date.desc())
            )
            reversed_entries = result.scalars().all()

            return [
                {
                    "original_id": entry.id,
                    "reversal_id": entry.reversal_entry_id,
                    "reversal_date": entry.reversed_date,
                    "reversed_by": entry.reversed_by,
                    "reversal_reason": entry.reversal_reason,
                    "amount": float(entry.total_debit),
                }
                for entry in reversed_entries
            ]
        except Exception as e:
            logger.error(f"Error getting reversals for period: {str(e)}")
            return []
    
    async def get_reversal_statistics(
        self,
        school_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Get statistics on reversals
        
        Args:
            school_id: School identifier
            start_date: Optional start date filter
            end_date: Optional end date filter
            
        Returns:
            Reversal statistics
        """
        try:
            query = select(JournalEntry).where(
                and_(
                    JournalEntry.school_id == school_id,
                    JournalEntry.posting_status == PostingStatus.REVERSED,
                )
            )
            
            if start_date:
                query = query.where(JournalEntry.reversed_date >= start_date)
            if end_date:
                query = query.where(JournalEntry.reversed_date <= end_date)
            
            result = await self.session.execute(query)
            reversed_entries = result.scalars().all()
            
            stats = {
                "total_reversals": len(reversed_entries),
                "total_amount_reversed": 0.0,
                "by_reason": {},
                "by_user": {},
                "by_reference_type": {},
            }
            
            for entry in reversed_entries:
                stats["total_amount_reversed"] += float(entry.total_debit)
                
                reason = entry.reversal_reason or "Unknown"
                if reason not in stats["by_reason"]:
                    stats["by_reason"][reason] = 0
                stats["by_reason"][reason] += 1
                
                user = entry.reversed_by or "Unknown"
                if user not in stats["by_user"]:
                    stats["by_user"][user] = 0
                stats["by_user"][user] += 1
                
                ref_type = entry.reference_type.value if entry.reference_type else "Unknown"
                if ref_type not in stats["by_reference_type"]:
                    stats["by_reference_type"][ref_type] = 0
                stats["by_reference_type"][ref_type] += 1
            
            return stats
        except Exception as e:
            logger.error(f"Error getting reversal statistics: {str(e)}")
            return {"error": str(e)}
