"""Financial statement generation — Trial Balance, Balance Sheet, Profit &
Loss, Cash Flow. Consumed by routers/finance/reports.py.

All balances are computed from POSTED journal_line_items joined to their
parent journal_entries (filtered on posting_status == POSTED and entry_date),
never from GLAccount.current_balance — that column is a denormalized
snapshot of "right now" and can't answer an as-of-date or period-range
query correctly.
"""
import logging
from datetime import datetime
from decimal import Decimal
from typing import Dict, Optional, Tuple

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.finance.chart_of_accounts import GLAccount, AccountType, AccountCategory
from models.finance.journal_entries import JournalEntry, JournalLineItem, PostingStatus
from models.finance.reports import (
    TrialBalanceReport, TrialBalanceLineItem,
    BalanceSheetReport, BalanceSheetSection, BalanceSheetSectionItem,
    ProfitLossReport, ProfitLossSection,
    CashFlowReport, CashFlowActivity, CashFlowActivityItem,
)

logger = logging.getLogger(__name__)


class ReportsServiceError(Exception):
    """Raised when a financial report can't be generated."""


class ReportsService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _account_movements(
        self, school_id: str, end_date: datetime, start_date: Optional[datetime] = None,
    ) -> Dict[str, Tuple[Decimal, Decimal]]:
        """gl_account_id -> (total_debit, total_credit) from entries that were
        actually posted to the GL. start_date=None means cumulative from
        inception through end_date (for balance-sheet-style point-in-time
        balances); start_date set means only movement within that date range
        (for P&L/cash-flow-style period activity).

        Includes REVERSED alongside POSTED: a reversed entry *was* posted —
        its reversal is a separate, also-posted contra-entry — so both legs'
        amounts must be included for them to net to zero together. Excluding
        the original once its status flips to REVERSED would leave only the
        contra-entry's amount showing (sign-flipped), not a net zero. DRAFT
        and REJECTED entries never touched the GL and stay excluded.
        """
        # journal_entries.entry_date is TIMESTAMP WITHOUT TIME ZONE — asyncpg
        # rejects comparing that against a tz-aware value (e.g. an ISO
        # datetime with a "Z"/offset from the API), so strip tzinfo here.
        end_date = end_date.replace(tzinfo=None) if end_date.tzinfo else end_date
        if start_date is not None:
            start_date = start_date.replace(tzinfo=None) if start_date.tzinfo else start_date

        conditions = [
            JournalEntry.school_id == school_id,
            JournalEntry.posting_status.in_([PostingStatus.POSTED, PostingStatus.REVERSED]),
            JournalEntry.entry_date <= end_date,
        ]
        if start_date is not None:
            conditions.append(JournalEntry.entry_date >= start_date)

        query = (
            select(
                JournalLineItem.gl_account_id,
                JournalLineItem.debit_amount,
                JournalLineItem.credit_amount,
            )
            .join(JournalEntry, JournalLineItem.journal_entry_id == JournalEntry.id)
            .where(and_(*conditions))
        )
        result = await self.session.execute(query)

        movements: Dict[str, Tuple[Decimal, Decimal]] = {}
        for gl_account_id, debit_amount, credit_amount in result.all():
            d, c = movements.get(gl_account_id, (Decimal("0"), Decimal("0")))
            movements[gl_account_id] = (d + (debit_amount or Decimal("0")), c + (credit_amount or Decimal("0")))
        return movements

    async def _active_accounts(self, school_id: str, account_type: Optional[AccountType] = None):
        query = select(GLAccount).where(GLAccount.school_id == school_id, GLAccount.is_active == True)  # noqa: E712
        if account_type:
            query = query.where(GLAccount.account_type == account_type)
        result = await self.session.execute(query.order_by(GLAccount.account_code))
        return result.scalars().all()

    @staticmethod
    def _signed_balance(account: GLAccount, debit: Decimal, credit: Decimal) -> Decimal:
        """Positive = a balance on the account's normal side."""
        if account.normal_balance == "credit":
            return credit - debit
        return debit - credit

    # ==================== Trial Balance ====================

    async def generate_trial_balance(self, school_id: str, as_of_date: datetime) -> TrialBalanceReport:
        try:
            movements = await self._account_movements(school_id, as_of_date)
            accounts = await self._active_accounts(school_id)

            line_items = []
            total_debits = Decimal("0")
            total_credits = Decimal("0")

            for account in accounts:
                debit, credit = movements.get(account.id, (Decimal("0"), Decimal("0")))
                if debit == 0 and credit == 0:
                    continue
                # Gross activity, not the net balance split across columns —
                # a $500 posting fully offset by its $500 reversal must still
                # show as $500 debited and $500 credited (it happened and was
                # undone), not as if nothing happened. Every entry balances
                # debit==credit on its own, so summing gross per account
                # still keeps the trial-balance-wide total_debits ==
                # total_credits identity intact.
                balance = self._signed_balance(account, debit, credit)

                total_debits += debit
                total_credits += credit

                line_items.append(TrialBalanceLineItem(
                    account_code=account.account_code,
                    account_name=account.account_name,
                    account_type=account.account_type.value,
                    normal_balance=account.normal_balance,
                    debit_amount=debit,
                    credit_amount=credit,
                    balance=balance,
                    closing_balance=balance,
                ))

            difference = total_debits - total_credits
            return TrialBalanceReport(
                school_id=school_id,
                as_of_date=as_of_date,
                line_items=line_items,
                total_debits=total_debits,
                total_credits=total_credits,
                difference=difference,
                is_balanced=abs(difference) < Decimal("0.01"),
            )
        except Exception as e:
            logger.error(f"Error generating trial balance for school {school_id}: {e}")
            raise ReportsServiceError(str(e))

    # ==================== Balance Sheet ====================

    async def _balance_sheet_section(
        self, school_id: str, as_of_date: datetime, movements: Dict[str, Tuple[Decimal, Decimal]],
        account_type: AccountType, section_name: str,
    ) -> BalanceSheetSection:
        accounts = await self._active_accounts(school_id, account_type)
        items = []
        section_total = Decimal("0")
        for account in accounts:
            debit, credit = movements.get(account.id, (Decimal("0"), Decimal("0")))
            balance = self._signed_balance(account, debit, credit)
            if balance == 0:
                continue
            items.append(BalanceSheetSectionItem(
                account_code=account.account_code, account_name=account.account_name, amount=abs(balance),
            ))
            section_total += balance
        return BalanceSheetSection(
            section_name=section_name, section_type=account_type.value, items=items, section_total=section_total,
        )

    async def generate_balance_sheet(self, school_id: str, as_of_date: datetime) -> BalanceSheetReport:
        try:
            movements = await self._account_movements(school_id, as_of_date)

            assets = await self._balance_sheet_section(school_id, as_of_date, movements, AccountType.ASSET, "Assets")
            liabilities = await self._balance_sheet_section(school_id, as_of_date, movements, AccountType.LIABILITY, "Liabilities")
            equity = await self._balance_sheet_section(school_id, as_of_date, movements, AccountType.EQUITY, "Equity")

            balance_difference = assets.section_total - (liabilities.section_total + equity.section_total)
            return BalanceSheetReport(
                school_id=school_id,
                as_of_date=as_of_date,
                assets=assets, liabilities=liabilities, equity=equity,
                total_assets=assets.section_total,
                total_liabilities=liabilities.section_total,
                total_equity=equity.section_total,
                is_balanced=abs(balance_difference) < Decimal("0.01"),
                balance_difference=balance_difference,
            )
        except Exception as e:
            logger.error(f"Error generating balance sheet for school {school_id}: {e}")
            raise ReportsServiceError(str(e))

    # ==================== Profit & Loss ====================

    async def generate_profit_loss(self, school_id: str, start_date: datetime, end_date: datetime) -> ProfitLossReport:
        try:
            movements = await self._account_movements(school_id, end_date, start_date)

            revenue_accounts = await self._active_accounts(school_id, AccountType.REVENUE)
            expense_accounts = await self._active_accounts(school_id, AccountType.EXPENSE)

            revenue_items, total_revenue = [], Decimal("0")
            for account in revenue_accounts:
                debit, credit = movements.get(account.id, (Decimal("0"), Decimal("0")))
                amount = credit - debit  # revenue is credit-normal
                if amount == 0:
                    continue
                revenue_items.append(BalanceSheetSectionItem(account_code=account.account_code, account_name=account.account_name, amount=amount))
                total_revenue += amount

            expense_items, total_expenses = [], Decimal("0")
            for account in expense_accounts:
                debit, credit = movements.get(account.id, (Decimal("0"), Decimal("0")))
                amount = debit - credit  # expense is debit-normal
                if amount == 0:
                    continue
                expense_items.append(BalanceSheetSectionItem(account_code=account.account_code, account_name=account.account_name, amount=amount))
                total_expenses += amount

            revenue_section = ProfitLossSection(section_name="Revenue", section_type="revenue", items=revenue_items, section_total=total_revenue)
            expenses_section = ProfitLossSection(section_name="Operating Expenses", section_type="operating_expenses", items=expense_items, section_total=total_expenses)
            operating_income = total_revenue - total_expenses

            return ProfitLossReport(
                school_id=school_id,
                period_start_date=start_date,
                period_end_date=end_date,
                revenue_section=revenue_section,
                operating_expenses_section=expenses_section,
                total_revenue=total_revenue,
                total_operating_expenses=total_expenses,
                operating_income=operating_income,
                net_income=operating_income,
            )
        except Exception as e:
            logger.error(f"Error generating P&L for school {school_id}: {e}")
            raise ReportsServiceError(str(e))

    # ==================== Cash Flow (simplified — see class docstring) ====================

    async def generate_cash_flow(self, school_id: str, start_date: datetime, end_date: datetime) -> CashFlowReport:
        """An indirect-method cash flow: net income, the depreciation
        add-back, and working-capital adjustments for the period's change in
        Accounts Receivable/Prepaid Expenses/Accounts Payable/Salaries
        Payable make up operating activity; net movement in fixed assets is
        investing; net movement in debt/equity is financing. Beginning/ending
        cash are read directly off BANK_ACCOUNTS balances (ground truth)
        rather than derived from the three activities, so any remaining gap
        between net_change_in_cash and (operating+investing+financing)
        points at a working-capital or other account category this report
        doesn't yet walk."""
        try:
            pl = await self.generate_profit_loss(school_id, start_date, end_date)
            period_movements = await self._account_movements(school_id, end_date, start_date)

            depreciation_accounts = [
                a for a in await self._active_accounts(school_id, AccountType.EXPENSE)
                if a.account_category == AccountCategory.DEPRECIATION
            ]
            depreciation_expense = Decimal("0")
            for account in depreciation_accounts:
                debit, credit = period_movements.get(account.id, (Decimal("0"), Decimal("0")))
                depreciation_expense += (debit - credit)

            operating_items = [CashFlowActivityItem(description="Net Income", amount=pl.net_income)]
            if depreciation_expense:
                operating_items.append(CashFlowActivityItem(description="Add back: Depreciation", amount=depreciation_expense))

            # Working-capital adjustments (indirect method): net income
            # includes revenue/expense recognized on an accrual basis, so a
            # change in AR/prepaid (asset, debit-normal — an increase is a
            # USE of cash) or AP/salaries payable (liability, credit-normal
            # — an increase is a SOURCE of cash) during the period has to be
            # backed out separately, or "cash from operations" silently
            # overstates/understates actual cash movement whenever a fee
            # invoice or expense accrual hasn't been collected/paid yet.
            working_capital_asset_categories = {AccountCategory.ACCOUNTS_RECEIVABLE, AccountCategory.PREPAID_EXPENSES}
            working_capital_liability_categories = {AccountCategory.ACCOUNTS_PAYABLE, AccountCategory.SALARIES_PAYABLE}

            working_capital_change = Decimal("0")
            for account in await self._active_accounts(school_id, AccountType.ASSET):
                if account.account_category not in working_capital_asset_categories:
                    continue
                debit, credit = period_movements.get(account.id, (Decimal("0"), Decimal("0")))
                increase = debit - credit
                if increase != 0:
                    working_capital_change -= increase
                    operating_items.append(CashFlowActivityItem(
                        description=f"({'Increase' if increase > 0 else 'Decrease'}) in {account.account_name}",
                        amount=-increase,
                    ))
            for account in await self._active_accounts(school_id, AccountType.LIABILITY):
                if account.account_category not in working_capital_liability_categories:
                    continue
                debit, credit = period_movements.get(account.id, (Decimal("0"), Decimal("0")))
                increase = credit - debit
                if increase != 0:
                    working_capital_change += increase
                    operating_items.append(CashFlowActivityItem(
                        description=f"Increase in {account.account_name}" if increase > 0 else f"Decrease in {account.account_name}",
                        amount=increase,
                    ))

            cash_from_operations = pl.net_income + depreciation_expense + working_capital_change
            operating_activities = CashFlowActivity(
                activity_type="operating", activity_name="Operating Activities",
                items=operating_items, activity_subtotal=cash_from_operations,
            )

            fixed_asset_accounts = [
                a for a in await self._active_accounts(school_id, AccountType.ASSET)
                if a.account_category == AccountCategory.FIXED_ASSETS
            ]
            fixed_asset_increase = Decimal("0")
            for account in fixed_asset_accounts:
                debit, credit = period_movements.get(account.id, (Decimal("0"), Decimal("0")))
                fixed_asset_increase += (debit - credit)
            cash_from_investing = -fixed_asset_increase
            investing_activities = CashFlowActivity(
                activity_type="investing", activity_name="Investing Activities",
                items=[CashFlowActivityItem(description="Net purchase of fixed assets", amount=cash_from_investing)],
                activity_subtotal=cash_from_investing,
            ) if fixed_asset_increase != 0 else None

            debt_categories = {AccountCategory.SHORT_TERM_DEBT, AccountCategory.LONG_TERM_DEBT}
            financing_accounts = [
                a for a in (await self._active_accounts(school_id, AccountType.LIABILITY))
                if a.account_category in debt_categories
            ] + [
                a for a in (await self._active_accounts(school_id, AccountType.EQUITY))
                if a.account_category == AccountCategory.ACCUMULATED_SURPLUS
            ]
            cash_from_financing = Decimal("0")
            for account in financing_accounts:
                debit, credit = period_movements.get(account.id, (Decimal("0"), Decimal("0")))
                cash_from_financing += self._signed_balance(account, debit, credit)
            financing_activities = CashFlowActivity(
                activity_type="financing", activity_name="Financing Activities",
                items=[CashFlowActivityItem(description="Net borrowing / owner contributions", amount=cash_from_financing)],
                activity_subtotal=cash_from_financing,
            ) if cash_from_financing != 0 else None

            cash_accounts = [
                a for a in await self._active_accounts(school_id, AccountType.ASSET)
                if a.account_category == AccountCategory.BANK_ACCOUNTS
            ]
            beginning_movements = await self._account_movements(school_id, start_date)
            ending_movements = await self._account_movements(school_id, end_date)
            beginning_cash = sum(
                (self._signed_balance(a, *beginning_movements.get(a.id, (Decimal("0"), Decimal("0")))) for a in cash_accounts),
                Decimal("0"),
            )
            ending_cash = sum(
                (self._signed_balance(a, *ending_movements.get(a.id, (Decimal("0"), Decimal("0")))) for a in cash_accounts),
                Decimal("0"),
            )

            return CashFlowReport(
                school_id=school_id,
                period_start_date=start_date,
                period_end_date=end_date,
                operating_activities=operating_activities,
                investing_activities=investing_activities,
                financing_activities=financing_activities,
                cash_from_operations=cash_from_operations,
                cash_from_investing=cash_from_investing,
                cash_from_financing=cash_from_financing,
                net_change_in_cash=ending_cash - beginning_cash,
                beginning_cash_balance=beginning_cash,
                ending_cash_balance=ending_cash,
            )
        except Exception as e:
            logger.error(f"Error generating cash flow for school {school_id}: {e}")
            raise ReportsServiceError(str(e))
