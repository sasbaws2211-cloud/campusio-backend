"""Retained Earnings Service - Auto-calculation at period close

Handles:
- Retained earnings calculation from P&L accounts
- Closing entry generation (revenue/expense → retained earnings)
- Opening balances for next period
- Equity account management
"""
import logging
from typing import Optional, Dict, Any, List, Tuple
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
from models.finance.chart_of_accounts import GLAccount, AccountType, AccountCategory
from models.finance.fiscal_period import FiscalPeriod, FiscalPeriodStatus
from models.finance.gl_audit_log import AuditActionType, AuditEntityType
from services.journal_entry_service import JournalEntryService, JournalEntryError
from services.coa_service import CoaService
from services.gl_audit_log_service import GLAuditLogService
from services.fiscal_period_service import FiscalPeriodService

logger = logging.getLogger(__name__)


class RetainedEarningsError(Exception):
    """Base exception for retained earnings operations"""
    pass


class RetainedEarningsService:
    """Service for managing retained earnings and period closing
    
    At period close:
    1. Calculate net income: Revenue - Expenses
    2. Close all P&L accounts to Retained Earnings
    3. Create opening balances for balance sheet accounts
    4. Generate post-closing trial balance (only balance sheet accounts)
    
    Ensures:
    - Equity = Assets - Liabilities (fundamental equation)
    - All revenue/expense accounts zero after close
    - Next period starts clean
    """
    
    def __init__(self, session: AsyncSession):
        """Initialize service with database session
        
        Args:
            session: AsyncSession for database operations
        """
        self.session = session
        self.journal_service = JournalEntryService(session)
        self.coa_service = CoaService(session)
        self.audit_service = GLAuditLogService(session)
        self.period_service = FiscalPeriodService(session)
    
    # ==================== Closing Procedures ====================

    async def _get_period_account_activity(
        self,
        school_id: str,
        period: FiscalPeriod,
        account_type: AccountType,
    ) -> List[Tuple[GLAccount, Decimal, Decimal]]:
        """Sum posted journal-entry activity for accounts of a type, bounded
        to a single fiscal period's date range.

        Closing must reflect exactly what happened *in this period* — not
        whatever the account's running `current_balance` happens to hold,
        which can include a late/backdated entry from outside the period, or
        drift if periods are closed out of chronological order. Only POSTED
        entries dated within [period.start_date, period.end_date] count.

        Returns:
            List of (account, period_debit_total, period_credit_total) for
            accounts with any activity in the period (zero-activity accounts
            are omitted).
        """
        result = await self.session.execute(
            select(
                GLAccount,
                func.coalesce(func.sum(JournalLineItem.debit_amount), Decimal("0")).label("period_debit"),
                func.coalesce(func.sum(JournalLineItem.credit_amount), Decimal("0")).label("period_credit"),
            )
            .join(JournalLineItem, JournalLineItem.gl_account_id == GLAccount.id)
            .join(JournalEntry, JournalEntry.id == JournalLineItem.journal_entry_id)
            .where(
                and_(
                    GLAccount.school_id == school_id,
                    GLAccount.account_type == account_type,
                    GLAccount.is_active == True,
                    # POSTED *and* REVERSED, not POSTED alone: reversing an
                    # entry flips its own status to REVERSED, so a
                    # POSTED-only filter would drop the original posting's
                    # line items while still counting the contra-entry's
                    # (which stays POSTED) — netting to the reversal amount
                    # instead of zero. Caught live via budget-vs-actual,
                    # which shares this exact query shape.
                    JournalEntry.posting_status.in_([PostingStatus.POSTED, PostingStatus.REVERSED]),
                    JournalEntry.entry_date >= period.start_date,
                    JournalEntry.entry_date <= period.end_date,
                )
            )
            .group_by(GLAccount.id)
        )

        return [
            (account, period_debit or Decimal("0"), period_credit or Decimal("0"))
            for account, period_debit, period_credit in result.all()
        ]

    async def calculate_net_income(
        self,
        school_id: str,
        period_id: str,
    ) -> Dict[str, Any]:
        """Calculate net income for a fiscal period

        Net Income = Revenue - Expenses, computed from this period's posted
        activity only (see _get_period_account_activity), not from accounts'
        cumulative current_balance.

        Args:
            school_id: School identifier
            period_id: Fiscal period ID

        Returns:
            Dictionary with revenue, expenses, and net income
        """
        try:
            # Get period
            period = await self.period_service.get_period_by_id(school_id, period_id)
            if not period:
                raise RetainedEarningsError(f"Period {period_id} not found")

            # Revenue is credit-normal: period contribution = credits - debits
            revenue_activity = await self._get_period_account_activity(school_id, period, AccountType.REVENUE)
            total_revenue = sum(credit - debit for _, debit, credit in revenue_activity)

            # Expense is debit-normal: period contribution = debits - credits
            expense_activity = await self._get_period_account_activity(school_id, period, AccountType.EXPENSE)
            total_expenses = sum(debit - credit for _, debit, credit in expense_activity)

            # Net income = Revenue - Expenses
            net_income = total_revenue - total_expenses

            return {
                "period_id": period_id,
                "period_name": period.period_name,
                "total_revenue": total_revenue,
                "total_expenses": total_expenses,
                "net_income": net_income,
                "is_profit": net_income >= 0,
            }
        except RetainedEarningsError:
            raise
        except Exception as e:
            logger.error(f"Error calculating net income: {str(e)}")
            raise RetainedEarningsError(f"Failed to calculate net income: {str(e)}")
    
    async def close_period(
        self,
        school_id: str,
        period_id: str,
        closed_by: str,
        ip_address: Optional[str] = None,
        user_role: str = "finance",
        user_name: str = "Unknown",
    ) -> Dict[str, Any]:
        """Close a fiscal period and create closing entries
        
        **CRITICAL OPERATION** - Closes P&L accounts, calculates retained earnings,
        and transitions period to CLOSED status.
        
        Args:
            school_id: School identifier
            period_id: Period to close
            closed_by: User closing the period
            ip_address: IP address for audit
            user_role: User role for audit
            
        Returns:
            Dictionary with closing results
            
        Raises:
            RetainedEarningsError: If closing cannot be completed
        """
        try:
            # Get period
            period = await self.period_service.get_period_by_id(school_id, period_id)
            if not period:
                raise RetainedEarningsError(f"Period {period_id} not found")
            
            if period.status != FiscalPeriodStatus.LOCKED:
                raise RetainedEarningsError(
                    f"Can only close LOCKED periods. Current status: {period.status.value}"
                )
            
            # Calculate net income
            net_income_data = await self.calculate_net_income(school_id, period_id)
            net_income = net_income_data["net_income"]

            # Get revenue/expense accounts with activity *in this period* (not
            # accounts' cumulative current_balance — see _get_period_account_activity)
            revenue_activity = await self._get_period_account_activity(school_id, period, AccountType.REVENUE)
            revenue_activity = [(acct, debit, credit) for acct, debit, credit in revenue_activity if abs(credit - debit) >= Decimal("0.01")]

            expense_activity = await self._get_period_account_activity(school_id, period, AccountType.EXPENSE)
            expense_activity = [(acct, debit, credit) for acct, debit, credit in expense_activity if abs(debit - credit) >= Decimal("0.01")]

            # Get retained earnings account — looked up by system_role so a
            # school can rename/replace account 3100 without breaking period
            # close (falls back to "3100" for schools seeded before
            # system_role existed).
            retained_earnings_account = await self.coa_service.get_system_account(
                school_id=school_id,
                system_role="retained_earnings",
                fallback_code="3100",
            )

            if not retained_earnings_account:
                raise RetainedEarningsError(
                    "No retained earnings account configured (expected a GL account "
                    "with system_role='retained_earnings' or account code 3100). "
                    "Please create it first."
                )
            
            # Create closing entry journal lines
            closing_lines = []

            # Close revenue accounts (credit-normal): debit them by this
            # period's net credit activity to zero out that contribution.
            # Amounts stay Decimal throughout — no float() round-trip right
            # before these get compared for balance.
            for revenue_account, period_debit, period_credit in revenue_activity:
                closing_lines.append(
                    JournalLineItemCreate(
                        gl_account_id=revenue_account.id,
                        debit_amount=period_credit - period_debit,
                        credit_amount=Decimal("0"),
                        description=f"Closing: {revenue_account.account_name}",
                        line_number=len(closing_lines) + 1,
                    )
                )

            # Close expense accounts (debit-normal): credit them by this
            # period's net debit activity to zero out that contribution.
            for expense_account, period_debit, period_credit in expense_activity:
                closing_lines.append(
                    JournalLineItemCreate(
                        gl_account_id=expense_account.id,
                        debit_amount=Decimal("0"),
                        credit_amount=period_debit - period_credit,
                        description=f"Closing: {expense_account.account_name}",
                        line_number=len(closing_lines) + 1,
                    )
                )

            # Post net income to retained earnings
            if net_income > 0:
                # Profit: credit retained earnings
                closing_lines.append(
                    JournalLineItemCreate(
                        gl_account_id=retained_earnings_account.id,
                        debit_amount=Decimal("0"),
                        credit_amount=net_income,
                        description=f"Net Income for {period.period_name}",
                        line_number=len(closing_lines) + 1,
                    )
                )
            elif net_income < 0:
                # Loss: debit retained earnings
                closing_lines.append(
                    JournalLineItemCreate(
                        gl_account_id=retained_earnings_account.id,
                        debit_amount=abs(net_income),
                        credit_amount=Decimal("0"),
                        description=f"Net Loss for {period.period_name}",
                        line_number=len(closing_lines) + 1,
                    )
                )
            
            # Create closing entry
            closing_entry_data = JournalEntryCreate(
                entry_date=period.end_date,
                reference_type=ReferenceType.ADJUSTMENT,
                reference_id=period_id,
                description=f"Period Close - {period.period_name}",
                line_items=closing_lines,
                notes=f"Auto-closing entry. Net income: {net_income:.2f}",
                # Must be able to post into the LOCKED period it's closing —
                # only adjusting entries are allowed past that lock (see
                # FiscalPeriodService.can_post_to_period).
                is_adjusting_entry=True,
            )
            
            # Create and post closing entry
            closing_entry = await self.journal_service.create_entry(
                school_id=school_id,
                entry_data=closing_entry_data,
                created_by="SYSTEM",
            )
            
            await self.journal_service.post_entry(
                school_id=school_id,
                entry_id=closing_entry.id,
                posted_by=closed_by,
                approval_notes=f"Period close for {period.period_name}",
                ip_address=ip_address,
                user_role=user_role,
            )
            
            # Update period status to CLOSED
            period.status = FiscalPeriodStatus.CLOSED
            period.closed_date = datetime.utcnow()
            period.closed_by = closed_by
            self.session.add(period)
            await self.session.commit()

            try:
                await self.audit_service.log_action(
                    school_id=school_id,
                    entity_type=AuditEntityType.FISCAL_PERIOD,
                    entity_id=period_id,
                    action=AuditActionType.PERIOD_CLOSED,
                    user_id=closed_by,
                    user_name=user_name,
                    user_role=user_role,
                    new_values={
                        "net_income": net_income,
                        "closing_entry_id": closing_entry.id,
                        "revenue_accounts_closed": len(revenue_activity),
                        "expense_accounts_closed": len(expense_activity),
                    },
                    ip_address=ip_address,
                )
            except Exception as e:
                logger.warning(f"Failed to write GL audit log for period close {period_id}: {e}")

            logger.info(
                f"Closed period {period.period_name} (net income: {net_income:.2f}, "
                f"closing entry: {closing_entry.id})"
            )

            # Auto-carry-forward opening balances into whatever period
            # immediately follows this one, so schools don't have to
            # remember set_opening_balances_for_period as a separate manual
            # step. Best-effort: a missing next period (not created yet) is
            # the normal case for the last period a school has set up, not
            # an error — the close itself has already succeeded and committed.
            opening_balances_result = None
            next_period_result = await self.session.execute(
                select(FiscalPeriod).where(
                    and_(
                        FiscalPeriod.school_id == school_id,
                        FiscalPeriod.start_date > period.end_date,
                    )
                ).order_by(FiscalPeriod.start_date.asc())
            )
            next_period = next_period_result.scalars().first()
            if next_period:
                try:
                    opening_balances_result = await self.set_opening_balances_for_period(
                        school_id=school_id,
                        from_period_id=period_id,
                        to_period_id=next_period.id,
                        created_by="SYSTEM",
                        user_role=user_role,
                        user_name="System (auto-carryforward on period close)",
                    )
                except Exception as e:
                    logger.warning(
                        f"Period {period_id} closed but auto opening-balance carryforward "
                        f"to {next_period.id} failed (can still be run manually): {e}"
                    )

            return {
                "status": "success",
                "period_id": period_id,
                "period_name": period.period_name,
                "net_income": net_income,
                "closing_entry_id": closing_entry.id,
                "revenue_accounts_closed": len(revenue_activity),
                "expense_accounts_closed": len(expense_activity),
                "opening_balances_carried_forward": opening_balances_result,
            }

        except RetainedEarningsError:
            await self.session.rollback()
            raise
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error closing period: {str(e)}")
            raise RetainedEarningsError(f"Failed to close period: {str(e)}")
    
    # ==================== Opening Balances ====================
    
    async def set_opening_balances_for_period(
        self,
        school_id: str,
        from_period_id: str,
        to_period_id: str,
        created_by: str,
        ip_address: Optional[str] = None,
        user_role: str = "finance",
        user_name: str = "Unknown",
    ) -> Dict[str, Any]:
        """Set opening balances for next period from previous period close
        
        Balance sheet accounts carry forward their closing balance.
        
        Args:
            school_id: School identifier
            from_period_id: Previous period ID
            to_period_id: New period ID
            created_by: User creating opening balances
            
        Returns:
            Summary of opening balances set
        """
        try:
            # Get to_period
            to_period = await self.period_service.get_period_by_id(school_id, to_period_id)
            if not to_period:
                raise RetainedEarningsError(f"Period {to_period_id} not found")
            
            # Get balance sheet accounts (Asset, Liability, Equity)
            bs_result = await self.session.execute(
                select(GLAccount).where(
                    and_(
                        GLAccount.school_id == school_id,
                        GLAccount.account_type.in_([
                            AccountType.ASSET,
                            AccountType.LIABILITY,
                            AccountType.EQUITY,
                        ]),
                        GLAccount.is_active == True,
                    )
                )
            )
            bs_accounts = bs_result.scalars().all()
            
            # Set opening balance for each (from current_balance)
            for account in bs_accounts:
                await self.coa_service.set_opening_balance(
                    school_id=school_id,
                    account_id=account.id,
                    opening_balance=account.current_balance,
                )
            
            try:
                await self.audit_service.log_action(
                    school_id=school_id,
                    entity_type=AuditEntityType.FISCAL_PERIOD,
                    entity_id=to_period_id,
                    action=AuditActionType.OPENING_BALANCE_IMPORTED,
                    user_id=created_by,
                    user_name=user_name,
                    user_role=user_role,
                    new_values={"from_period_id": from_period_id, "accounts_updated": len(bs_accounts)},
                    ip_address=ip_address,
                )
            except Exception as e:
                logger.warning(f"Failed to write GL audit log for opening balances {to_period_id}: {e}")

            logger.info(
                f"Set opening balances for {len(bs_accounts)} balance sheet accounts "
                f"for period {to_period.period_name}"
            )

            return {
                "period_id": to_period_id,
                "accounts_updated": len(bs_accounts),
                "timestamp": datetime.utcnow().isoformat(),
            }
            
        except RetainedEarningsError:
            raise
        except Exception as e:
            logger.error(f"Error setting opening balances: {str(e)}")
            raise RetainedEarningsError(f"Failed to set opening balances: {str(e)}")
    
    # ==================== Trial Balance & Analysis ====================
    
    async def get_post_closing_trial_balance(
        self,
        school_id: str,
        period_id: str,
    ) -> Dict[str, Any]:
        """Get post-closing trial balance (balance sheet accounts only)
        
        After period close, only balance sheet accounts should have balances.
        Revenue and expense accounts should be zero.
        
        Args:
            school_id: School identifier
            period_id: Period ID
            
        Returns:
            Post-closing trial balance
        """
        try:
            # Get balance sheet accounts with balances
            result = await self.session.execute(
                select(GLAccount).where(
                    and_(
                        GLAccount.school_id == school_id,
                        GLAccount.account_type.in_([
                            AccountType.ASSET,
                            AccountType.LIABILITY,
                            AccountType.EQUITY,
                        ]),
                        GLAccount.is_active == True,
                        GLAccount.current_balance != 0,
                    )
                ).order_by(GLAccount.account_type, GLAccount.account_code)
            )
            accounts = result.scalars().all()
            
            total_debits = Decimal("0")
            total_credits = Decimal("0")
            by_type = {}

            for account in accounts:
                account_type = account.account_type.value
                if account_type not in by_type:
                    by_type[account_type] = {
                        "accounts": [],
                        "debit": Decimal("0"),
                        "credit": Decimal("0"),
                    }

                if account.current_balance > 0:
                    debit = account.current_balance
                    credit = Decimal("0")
                    total_debits += debit
                else:
                    debit = Decimal("0")
                    credit = abs(account.current_balance)
                    total_credits += credit
                
                by_type[account_type]["accounts"].append({
                    "code": account.account_code,
                    "name": account.account_name,
                    "debit": debit,
                    "credit": credit,
                })
                by_type[account_type]["debit"] += debit
                by_type[account_type]["credit"] += credit
            
            # Verify trial balance is balanced
            balanced = abs(total_debits - total_credits) < Decimal("0.01")
            
            return {
                "period_id": period_id,
                "total_debits": total_debits,
                "total_credits": total_credits,
                "balanced": balanced,
                "by_type": by_type,
                "timestamp": datetime.utcnow().isoformat(),
            }
            
        except Exception as e:
            logger.error(f"Error getting post-closing trial balance: {str(e)}")
            return {"error": str(e)}
    
    async def verify_period_closed(
        self,
        school_id: str,
        period_id: str,
    ) -> Dict[str, Any]:
        """Verify a period is properly closed
        
        Checks:
        - All revenue/expense accounts are zero
        - Trial balance is balanced
        - Period status is CLOSED
        
        Args:
            school_id: School identifier
            period_id: Period ID to verify
            
        Returns:
            Verification results
        """
        try:
            # Get period
            period = await self.period_service.get_period_by_id(school_id, period_id)
            if not period:
                return {"error": "Period not found"}
            
            # Check period is closed
            is_closed = period.status == FiscalPeriodStatus.CLOSED
            
            # Check P&L accounts are zero
            pl_result = await self.session.execute(
                select(GLAccount).where(
                    and_(
                        GLAccount.school_id == school_id,
                        GLAccount.account_type.in_([
                            AccountType.REVENUE,
                            AccountType.EXPENSE,
                        ]),
                        GLAccount.is_active == True,
                        GLAccount.current_balance != 0,
                    )
                )
            )
            unclosed_accounts = pl_result.scalars().all()
            
            # Get trial balance
            tb = await self.get_post_closing_trial_balance(school_id, period_id)
            
            issues = []
            if not is_closed:
                issues.append("Period status is not CLOSED")
            if unclosed_accounts:
                issues.append(
                    f"{len(unclosed_accounts)} P&L accounts still have balances"
                )
            if not tb.get("balanced"):
                issues.append("Trial balance is not balanced")
            
            return {
                "period_id": period_id,
                "period_name": period.period_name,
                "status": period.status.value,
                "is_properly_closed": len(issues) == 0,
                "issues": issues,
                "trial_balance_balanced": tb.get("balanced"),
                "unclosed_accounts": len(unclosed_accounts),
            }
            
        except Exception as e:
            logger.error(f"Error verifying period close: {str(e)}")
            return {"error": str(e)}
    
    async def get_retained_earnings_balance(
        self,
        school_id: str,
    ) -> float:
        """Get current retained earnings balance
        
        Args:
            school_id: School identifier
            
        Returns:
            Retained earnings balance
        """
        try:
            account = await self.coa_service.get_system_account(
                school_id=school_id,
                system_role="retained_earnings",
                fallback_code="3100",
            )
            return float(account.current_balance) if account else 0.0
        except Exception as e:
            logger.error(f"Error getting retained earnings balance: {str(e)}")
            return 0.0
