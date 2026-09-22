"""Chart of Accounts Service - Business logic for GL account management"""
import logging
from typing import Optional, List, Dict, Any
from decimal import Decimal
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, and_, or_
from datetime import datetime

from models.finance.chart_of_accounts import (
    GLAccount,
    GLAccountCreate,
    GLAccountUpdate,
    AccountType,
    AccountCategory,
)

logger = logging.getLogger(__name__)

# Standard accounting normal-balance rule by account type. Contra accounts (e.g. Accumulated
# Depreciation, an ASSET that normally carries a credit balance) override this via an explicit
# normal_balance on the request — this is just the default when none is given.
DEFAULT_NORMAL_BALANCE_BY_TYPE = {
    AccountType.ASSET: "debit",
    AccountType.EXPENSE: "debit",
    AccountType.LIABILITY: "credit",
    AccountType.EQUITY: "credit",
    AccountType.REVENUE: "credit",
}


class CoaServiceError(Exception):
    """Base exception for Chart of Accounts service errors"""
    pass


class CoaService:
    """Service for managing Chart of Accounts operations
    
    Handles:
    - GL account CRUD operations
    - Account code validation and uniqueness
    - Hierarchical account relationships
    - Account type and category queries
    - Activation/deactivation of accounts
    """
    
    def __init__(self, session: AsyncSession):
        """Initialize service with async database session
        
        Args:
            session: AsyncSession for database operations
        """
        self.session = session
    
    # ==================== Create Operations ====================
    
    async def create_account(
        self,
        school_id: str,
        account_data: GLAccountCreate,
        created_by: str
    ) -> GLAccount:
        """Create a new GL account
        
        Args:
            school_id: School identifier for multi-tenancy
            account_data: Account creation data
            created_by: User ID creating the account
            
        Returns:
            Created GLAccount instance
            
        Raises:
            CoaServiceError: If account code already exists for school
        """
        # Check uniqueness of account code within school
        existing = await self._get_account_by_code(school_id, account_data.account_code)
        if existing:
            raise CoaServiceError(
                f"Account code '{account_data.account_code}' already exists for this school"
            )
        
        # Validate parent account exists if specified
        if account_data.parent_account_id:
            parent = await self.get_account_by_id(school_id, account_data.parent_account_id)
            if not parent:
                raise CoaServiceError(f"Parent account '{account_data.parent_account_id}' not found")
        
        normal_balance = account_data.normal_balance or DEFAULT_NORMAL_BALANCE_BY_TYPE[account_data.account_type]

        # Create new account
        account = GLAccount(
            school_id=school_id,
            account_code=account_data.account_code,
            account_name=account_data.account_name,
            account_type=account_data.account_type,
            account_category=account_data.account_category,
            description=account_data.description,
            normal_balance=normal_balance,
            parent_account_id=account_data.parent_account_id,
            system_role=account_data.system_role,
            created_by=created_by,
            is_active=True,
        )
        
        self.session.add(account)
        await self.session.commit()
        await self.session.refresh(account)
        
        logger.info(
            f"Created GL account {account.account_code} ({account.account_name}) "
            f"for school {school_id}"
        )
        
        return account
    
    # ==================== Read Operations ====================
    
    async def get_account_by_id(self, school_id: str, account_id: str) -> Optional[GLAccount]:
        """Get account by ID for a specific school
        
        Args:
            school_id: School identifier
            account_id: Account ID to retrieve
            
        Returns:
            GLAccount if found, None otherwise
        """
        try:
            result = await self.session.execute(
                select(GLAccount).where(
                    and_(
                        GLAccount.school_id == school_id,
                        GLAccount.id == account_id
                    )
                )
            )
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Error fetching account {account_id}: {str(e)}")
            return None
    
    async def get_account_by_code(self, school_id: str, account_code: str) -> Optional[GLAccount]:
        """Get account by account code for a specific school
        
        Args:
            school_id: School identifier
            account_code: Account code (e.g., "1010", "5100")
            
        Returns:
            GLAccount if found, None otherwise
        """
        return await self._get_account_by_code(school_id, account_code)
    
    async def _get_account_by_code(self, school_id: str, account_code: str) -> Optional[GLAccount]:
        """Internal method to get account by code
        
        Args:
            school_id: School identifier
            account_code: Account code
            
        Returns:
            GLAccount if found, None otherwise
        """
        try:
            result = await self.session.execute(
                select(GLAccount).where(
                    and_(
                        GLAccount.school_id == school_id,
                        GLAccount.account_code == account_code
                    )
                )
            )
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Error fetching account by code {account_code}: {str(e)}")
            return None
    
    async def get_system_account(
        self,
        school_id: str,
        system_role: str,
        fallback_code: Optional[str] = None,
    ) -> Optional[GLAccount]:
        """Look up a well-known account by its system role (e.g.
        "default_cash_account", "retained_earnings") instead of a hardcoded
        account_code.

        Falls back to `fallback_code` when no account is tagged with that
        role yet — e.g. schools seeded before system_role existed — so this
        stays backward-compatible while letting a school reassign which
        account fills a system role without breaking expense posting or
        period close on a renamed/deleted hardcoded code.

        Args:
            school_id: School identifier
            system_role: Role to look up (e.g. "default_cash_account")
            fallback_code: Account code to fall back to if no account has
                this system_role set

        Returns:
            GLAccount if found (by role or fallback code), else None
        """
        try:
            result = await self.session.execute(
                select(GLAccount).where(
                    and_(
                        GLAccount.school_id == school_id,
                        GLAccount.system_role == system_role,
                        GLAccount.is_active == True,
                    )
                )
            )
            account = result.scalar_one_or_none()
            if account:
                return account
        except Exception as e:
            logger.error(f"Error looking up system account '{system_role}' for school {school_id}: {str(e)}")

        if fallback_code:
            return await self._get_account_by_code(school_id, fallback_code)
        return None

    async def get_all_accounts(
        self,
        school_id: str,
        active_only: bool = True,
        account_type: Optional[AccountType] = None,
        account_category: Optional[AccountCategory] = None,
    ) -> List[GLAccount]:
        """Get all GL accounts with optional filtering
        
        Args:
            school_id: School identifier
            active_only: Filter to active accounts only (default: True)
            account_type: Filter by account type (optional)
            account_category: Filter by account category (optional)
            
        Returns:
            List of GLAccount instances
        """
        try:
            query = select(GLAccount).where(GLAccount.school_id == school_id)
            
            if active_only:
                query = query.where(GLAccount.is_active == True)
            
            if account_type:
                query = query.where(GLAccount.account_type == account_type)
            
            if account_category:
                query = query.where(GLAccount.account_category == account_category)
            
            # Order by account code for easy scanning
            query = query.order_by(GLAccount.account_code)
            
            result = await self.session.execute(query)
            return result.scalars().all()
        except Exception as e:
            logger.error(f"Error fetching accounts for school {school_id}: {str(e)}")
            return []
    
    async def get_accounts_by_type(
        self,
        school_id: str,
        account_type: AccountType,
        active_only: bool = True,
    ) -> List[GLAccount]:
        """Get all accounts of a specific type
        
        Args:
            school_id: School identifier
            account_type: Account type to filter by
            active_only: Filter to active accounts only
            
        Returns:
            List of GLAccount instances
        """
        return await self.get_all_accounts(
            school_id=school_id,
            active_only=active_only,
            account_type=account_type
        )
    
    async def get_accounts_by_category(
        self,
        school_id: str,
        account_category: AccountCategory,
        active_only: bool = True,
    ) -> List[GLAccount]:
        """Get all accounts in a specific category
        
        Args:
            school_id: School identifier
            account_category: Account category to filter by
            active_only: Filter to active accounts only
            
        Returns:
            List of GLAccount instances
        """
        return await self.get_all_accounts(
            school_id=school_id,
            active_only=active_only,
            account_category=account_category
        )
    
    async def get_sub_accounts(
        self,
        school_id: str,
        parent_account_id: str,
        active_only: bool = True,
    ) -> List[GLAccount]:
        """Get all sub-accounts under a parent account (hierarchical)
        
        Args:
            school_id: School identifier
            parent_account_id: Parent account ID
            active_only: Filter to active accounts only
            
        Returns:
            List of child GLAccount instances
        """
        try:
            query = select(GLAccount).where(
                and_(
                    GLAccount.school_id == school_id,
                    GLAccount.parent_account_id == parent_account_id
                )
            )
            
            if active_only:
                query = query.where(GLAccount.is_active == True)
            
            query = query.order_by(GLAccount.account_code)
            
            result = await self.session.execute(query)
            return result.scalars().all()
        except Exception as e:
            logger.error(f"Error fetching sub-accounts for {parent_account_id}: {str(e)}")
            return []
    
    # ==================== Update Operations ====================
    
    async def update_account(
        self,
        school_id: str,
        account_id: str,
        update_data: GLAccountUpdate,
    ) -> Optional[GLAccount]:
        """Update an existing GL account
        
        Args:
            school_id: School identifier
            account_id: Account ID to update
            update_data: Fields to update
            
        Returns:
            Updated GLAccount instance, or None if not found
        """
        account = await self.get_account_by_id(school_id, account_id)
        if not account:
            logger.warning(f"Attempt to update non-existent account {account_id}")
            return None
        
        # Update fields that are provided
        update_dict = update_data.model_dump(exclude_unset=True)
        
        for key, value in update_dict.items():
            setattr(account, key, value)
        
        account.updated_at = datetime.utcnow()
        
        self.session.add(account)
        await self.session.commit()
        await self.session.refresh(account)
        
        logger.info(f"Updated GL account {account.account_code} for school {school_id}")
        
        return account
    
    # ==================== Deactivation Operations ====================
    
    async def deactivate_account(self, school_id: str, account_id: str) -> Optional[GLAccount]:
        """Deactivate (soft delete) a GL account
        
        Deactivation is used instead of hard delete to maintain audit trail and prevent
        orphaned journal entries from referencing deleted accounts.
        
        Args:
            school_id: School identifier
            account_id: Account ID to deactivate
            
        Returns:
            Deactivated GLAccount instance, or None if not found
        """
        account = await self.get_account_by_id(school_id, account_id)
        if not account:
            return None

        # Every report (Trial Balance, Balance Sheet, P&L via
        # services/reports_service.py::_active_accounts) filters to
        # is_active == True — deactivating an account with a nonzero
        # balance would silently drop that balance from every financial
        # statement with no error anywhere, breaking Assets == Liabilities
        # + Equity from the very next report run.
        if account.current_balance != 0:
            raise CoaServiceError(
                f"Cannot deactivate account '{account.account_code}' — it has a nonzero balance "
                f"({account.current_balance}). Transfer or zero out the balance first."
            )

        account.is_active = False
        account.updated_at = datetime.utcnow()
        
        self.session.add(account)
        await self.session.commit()
        await self.session.refresh(account)
        
        logger.info(f"Deactivated GL account {account.account_code} for school {school_id}")
        
        return account
    
    async def activate_account(self, school_id: str, account_id: str) -> Optional[GLAccount]:
        """Reactivate a previously deactivated GL account
        
        Args:
            school_id: School identifier
            account_id: Account ID to reactivate
            
        Returns:
            Reactivated GLAccount instance, or None if not found
        """
        account = await self.get_account_by_id(school_id, account_id)
        if not account:
            return None
        
        account.is_active = True
        account.updated_at = datetime.utcnow()
        
        self.session.add(account)
        await self.session.commit()
        await self.session.refresh(account)
        
        logger.info(f"Reactivated GL account {account.account_code} for school {school_id}")
        
        return account
    
    # ==================== Validation & Analysis Operations ====================
    
    async def validate_account_code_exists(self, school_id: str, account_code: str) -> bool:
        """Check if an account code exists for a school
        
        Args:
            school_id: School identifier
            account_code: Account code to check
            
        Returns:
            True if account exists, False otherwise
        """
        account = await self._get_account_by_code(school_id, account_code)
        return account is not None
    
    async def get_account_balance_summary(self, school_id: str) -> Dict[str, Any]:
        """Get summary of accounts by type and category
        
        Useful for dashboard overviews and validation checks.
        
        Args:
            school_id: School identifier
            
        Returns:
            Dictionary with account counts and totals by type/category
        """
        try:
            all_accounts = await self.get_all_accounts(school_id, active_only=True)
            
            summary = {
                "total_accounts": len(all_accounts),
                "by_type": {},
                "by_category": {},
            }
            
            # Count by account type
            for account_type in AccountType:
                count = sum(1 for acc in all_accounts if acc.account_type == account_type)
                if count > 0:
                    summary["by_type"][account_type.value] = count
            
            # Count by account category
            for account_category in AccountCategory:
                count = sum(1 for acc in all_accounts if acc.account_category == account_category)
                if count > 0:
                    summary["by_category"][account_category.value] = count
            
            return summary
        except Exception as e:
            logger.error(f"Error generating account summary for school {school_id}: {str(e)}")
            return {"error": str(e)}
    
    # ==================== ⭐ BALANCE TRACKING OPERATIONS (CRITICAL) ====================
    
    async def update_account_balance(
        self,
        school_id: str,
        account_id: str,
        debit_amount: Decimal = Decimal("0"),
        credit_amount: Decimal = Decimal("0"),
        commit: bool = True,
    ) -> Optional[GLAccount]:
        """Update GL account balance after posting a transaction

        This is CRITICAL for performance and accuracy. Balances are denormalized
        and cached in the GL account record. This method is called during:
        - Journal entry posting
        - Expense posting
        - Period close procedures

        Balance calculation:
        - For DEBIT normal accounts (assets, expenses): DR+ / CR-
        - For CREDIT normal accounts (liabilities, revenue, equity): CR+ / DR-

        The balance is updated via an atomic `SET current_balance = current_balance
        + delta` rather than a Python read-modify-write, so two concurrent postings
        to the same account can't race and silently lose one update.

        Args:
            school_id: School identifier
            account_id: Account ID to update
            debit_amount: Debit amount to add to balance
            credit_amount: Credit amount to add to balance
            commit: If False, stage the change (flush only) and let the caller
                commit — used by multi-line postings so the whole entry posts
                as one transaction instead of one commit per line item.

        Returns:
            Updated GLAccount, or None if not found
        """
        try:
            account = await self.get_account_by_id(school_id, account_id)
            if not account:
                logger.warning(f"Cannot update balance for non-existent account {account_id}")
                return None

            # Calculate balance change based on normal balance
            if account.normal_balance == "debit":
                # Debit normal: DR increases, CR decreases
                balance_change = debit_amount - credit_amount
            else:
                # Credit normal: CR increases, DR decreases
                balance_change = credit_amount - debit_amount

            now = datetime.utcnow()
            await self.session.execute(
                sql_update(GLAccount)
                .where(
                    and_(
                        GLAccount.school_id == school_id,
                        GLAccount.id == account_id,
                    )
                )
                .values(
                    current_balance=GLAccount.current_balance + balance_change,
                    last_balance_update=now,
                    updated_at=now,
                )
                # Without this, SQLAlchemy's default synchronize_session="auto"
                # evaluates the arithmetic SET clause itself and bumps the
                # already-loaded `account` object's current_balance in-memory
                # to match — then the manual `account.current_balance +=
                # balance_change` below double-applies the same delta.
                .execution_options(synchronize_session=False)
            )

            if commit:
                await self.session.commit()
                await self.session.refresh(account)
            else:
                await self.session.flush()
                # Reflect the change on the in-memory object without a round
                # trip — the caller owns the commit/refresh at this point.
                account.current_balance += balance_change
                account.last_balance_update = now
                account.updated_at = now

            logger.debug(
                f"Updated balance for account {account.account_code}: "
                f"DR {debit_amount} CR {credit_amount} → Balance {account.current_balance}"
            )

            return account
        except Exception as e:
            logger.error(f"Error updating account balance for {account_id}: {str(e)}")
            if commit:
                return None
            # In the deferred-commit path, the caller (post_entry) is
            # responsible for rolling back and surfacing the failure —
            # swallowing it here would let posting continue with a
            # partially-staged transaction.
            raise
    
    async def recalculate_account_balance(
        self,
        school_id: str,
        account_id: str,
        from_journal_entries: bool = True,
    ) -> Optional[GLAccount]:
        """Recalculate GL account balance from journal entries
        
        This method recalculates the balance by summing all posted journal entries
        for the account. Useful for:
        - Verification after balance update issues
        - Periodic reconciliation
        - Period-end procedures
        
        Args:
            school_id: School identifier
            account_id: Account ID to recalculate
            from_journal_entries: If True, recalc from JEs; if False use cached
            
        Returns:
            Updated GLAccount with recalculated balance
        """
        from sqlmodel import func
        from models.finance.journal_entries import JournalLineItem, JournalEntry, PostingStatus
        
        try:
            account = await self.get_account_by_id(school_id, account_id)
            if not account:
                return None
            
            if not from_journal_entries:
                return account  # Use existing cached balance
            
            # Sum all debits and credits for this account from POSTED entries
            result = await self.session.execute(
                select(
                    func.sum(JournalLineItem.debit_amount).label("total_debits"),
                    func.sum(JournalLineItem.credit_amount).label("total_credits"),
                ).join(
                    JournalEntry,
                    JournalLineItem.journal_entry_id == JournalEntry.id
                ).where(
                    and_(
                        JournalLineItem.school_id == school_id,
                        JournalLineItem.gl_account_id == account_id,
                        # POSTED *and* REVERSED, not POSTED alone: reversing an
                        # entry flips its own status to REVERSED, so a
                        # POSTED-only filter would drop the original posting's
                        # line items while still counting the contra-entry's —
                        # netting to the reversal amount instead of zero.
                        JournalEntry.posting_status.in_([PostingStatus.POSTED, PostingStatus.REVERSED]),
                    )
                )
            )
            
            row = result.first()
            total_debits = Decimal(str(row[0])) if row[0] is not None else Decimal("0")
            total_credits = Decimal(str(row[1])) if row[1] is not None else Decimal("0")

            # Calculate balance based on normal balance
            if account.normal_balance == "debit":
                account.current_balance = total_debits - total_credits
            else:
                account.current_balance = total_credits - total_debits
            
            account.last_balance_update = datetime.utcnow()
            account.updated_at = datetime.utcnow()
            
            self.session.add(account)
            await self.session.commit()
            await self.session.refresh(account)
            
            logger.info(
                f"Recalculated balance for {account.account_code}: "
                f"DR {total_debits} CR {total_credits} → Balance {account.current_balance}"
            )
            
            return account
        except Exception as e:
            logger.error(f"Error recalculating balance for account {account_id}: {str(e)}")
            return None
    
    async def recalculate_all_balances(self, school_id: str) -> Dict[str, Any]:
        """Batch recalculate all GL account balances
        
        This is a heavy operation used during:
        - Data correction/audit procedures
        - Period close verification
        - Balance reconciliation
        
        Should be run during low-traffic times. Returns summary of changes.
        
        Args:
            school_id: School identifier
            
        Returns:
            Summary dictionary with counts and results
        """
        try:
            all_accounts = await self.get_all_accounts(school_id, active_only=False)
            
            summary = {
                "total_accounts": len(all_accounts),
                "recalculated": 0,
                "errors": 0,
                "total_balance": Decimal("0"),
                "by_type": {},
            }

            for account in all_accounts:
                result = await self.recalculate_account_balance(
                    school_id, account.id, from_journal_entries=True
                )

                if result:
                    summary["recalculated"] += 1
                    summary["total_balance"] += result.current_balance

                    account_type = result.account_type.value
                    if account_type not in summary["by_type"]:
                        summary["by_type"][account_type] = {
                            "count": 0,
                            "total_balance": Decimal("0"),
                        }
                    summary["by_type"][account_type]["count"] += 1
                    summary["by_type"][account_type]["total_balance"] += result.current_balance
                else:
                    summary["errors"] += 1
            
            logger.info(
                f"Recalculated {summary['recalculated']} GL account balances for school {school_id} "
                f"(Total balance: {summary['total_balance']}, Errors: {summary['errors']})"
            )
            
            return summary
        except Exception as e:
            logger.error(f"Error recalculating all balances for school {school_id}: {str(e)}")
            return {"error": str(e)}
    
    async def get_account_balance(
        self,
        school_id: str,
        account_id: str,
        use_cached: bool = True,
    ) -> Optional[Decimal]:
        """Get current balance of a GL account
        
        Uses cached balance by default (performance). Pass use_cached=False
        to recalculate from journal entries for verification.
        
        Args:
            school_id: School identifier
            account_id: Account ID
            use_cached: If True use denormalized balance, else recalculate
            
        Returns:
            Current balance, or None if account not found
        """
        account = await self.get_account_by_id(school_id, account_id)
        if not account:
            return None
        
        if use_cached:
            return account.current_balance
        
        # Recalculate
        result = await self.recalculate_account_balance(
            school_id, account_id, from_journal_entries=True
        )
        return result.current_balance if result else None
    
    async def set_opening_balance(
        self,
        school_id: str,
        account_id: str,
        opening_balance: Decimal,
    ) -> Optional[GLAccount]:
        """Set opening balance for GL account (for period tracking)

        Opening balance is set at the start of each period and used for:
        - Period comparisons (opening → closing)
        - Year-to-date calculations
        - Period-over-period analysis

        Args:
            school_id: School identifier
            account_id: Account ID
            opening_balance: Balance at period start

        Returns:
            Updated GLAccount, or None if not found
        """
        account = await self.get_account_by_id(school_id, account_id)
        if not account:
            return None

        # Accept a plain float too (some callers still pass one) without
        # losing precision the way `Decimal(float)` would.
        account.opening_balance = (
            opening_balance if isinstance(opening_balance, Decimal) else Decimal(str(opening_balance))
        )
        account.updated_at = datetime.utcnow()
        
        self.session.add(account)
        await self.session.commit()
        await self.session.refresh(account)
        
        logger.info(
            f"Set opening balance for account {account.account_code}: {opening_balance}"
        )
        
        return account
    
    async def record_bank_reconciliation(
        self,
        school_id: str,
        account_id: str,
        reconciled_balance: float,
        reconciliation_date: datetime,
        notes: Optional[str] = None,
    ) -> Optional[GLAccount]:
        """Record bank reconciliation for a GL account
        
        Stores the reconciled balance and date for audit trail.
        Used during bank reconciliation procedures.
        
        Args:
            school_id: School identifier
            account_id: Account ID (usually a bank account GL)
            reconciled_balance: Balance per bank statement
            reconciliation_date: When reconciliation was done
            notes: Optional notes about the reconciliation
            
        Returns:
            Updated GLAccount, or None if not found
        """
        account = await self.get_account_by_id(school_id, account_id)
        if not account:
            return None

        # The bank-reconciliation subsystem still deals in float — convert
        # at this boundary (via str, not a direct Decimal(float) cast, to
        # avoid inheriting the float's binary-rounding noise) rather than
        # widening Decimal through that whole subsystem.
        account.bank_reconciled_balance = Decimal(str(reconciled_balance))
        account.bank_reconciliation_date = reconciliation_date
        account.reconciliation_notes = notes
        account.updated_at = datetime.utcnow()
        
        self.session.add(account)
        await self.session.commit()
        await self.session.refresh(account)
        
        logger.info(
            f"Recorded bank reconciliation for {account.account_code}: "
            f"Balance {reconciled_balance} as of {reconciliation_date.date()}"
        )
        
        return account

