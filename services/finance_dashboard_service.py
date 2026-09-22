"""Shared GL-balance-derived financial snapshot — the single implementation
behind both routers/finance_reports.py::get_dashboard_metrics (FinancePage.js)
and routers/dashboard.py::get_dashboard_overview (the main post-login
DashboardPage.js). Previously only the former computed true accrual-based
revenue/profit from posted journal entries; the latter had no revenue figure
at all. Extracted here so there's exactly one calculation, not two that could
silently drift apart.

Calculates metrics from posted journal entries to GL accounts using proper
accounting balance rules:
- Assets/Expenses: Debit - Credit
- Liabilities/Equity/Revenue: Credit - Debit
"""
import logging

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func
from sqlmodel import select

from models.finance import GLAccount, JournalLineItem, JournalEntry, PostingStatus, AccountType

logger = logging.getLogger(__name__)

_ZERO_SNAPSHOT = {
    "cash_balance": 0.0,
    "accounts_receivable": 0.0,
    "total_assets": 0.0,
    "accounts_payable": 0.0,
    "net_profit": 0.0,
    "profit_margin": 0.0,
    "revenue_by_source": [],
}


async def compute_financial_snapshot(session: AsyncSession, school_id: str) -> dict:
    """Returns cash_balance, accounts_receivable, total_assets,
    accounts_payable, net_profit, profit_margin, and revenue_by_source
    (a list of {name, amount, percentage}) — all keys always present.

    Never raises: any error, or a school with no GL accounts / no posted
    journal entries yet, returns an all-zero snapshot so callers can render
    "not configured yet" instead of failing the whole dashboard/report call.
    """
    try:
        account_query = select(GLAccount).where(
            GLAccount.school_id == school_id,
            GLAccount.is_active == True,  # noqa: E712
        )
        gl_accounts = {acc.id: acc for acc in (await session.execute(account_query)).scalars().all()}
        if not gl_accounts:
            return dict(_ZERO_SNAPSHOT)

        # POSTED *and* REVERSED, not POSTED alone: reversing an entry flips
        # its own status to REVERSED, so a POSTED-only filter would drop the
        # original posting's line items while still counting the
        # contra-entry's — netting to the reversal amount instead of zero.
        posted_entries_query = select(JournalEntry.id).where(
            JournalEntry.school_id == school_id,
            JournalEntry.posting_status.in_([PostingStatus.POSTED, PostingStatus.REVERSED]),
        )
        posted_entry_ids = (await session.execute(posted_entries_query)).scalars().all()
        if not posted_entry_ids:
            return dict(_ZERO_SNAPSHOT)

        line_items_query = select(
            JournalLineItem.gl_account_id,
            func.sum(JournalLineItem.debit_amount).label("total_debit"),
            func.sum(JournalLineItem.credit_amount).label("total_credit"),
        ).where(
            JournalLineItem.journal_entry_id.in_(posted_entry_ids)
        ).group_by(JournalLineItem.gl_account_id)

        account_balances = {
            gl_account_id: {"debit": total_debit or 0, "credit": total_credit or 0}
            for gl_account_id, total_debit, total_credit in (await session.execute(line_items_query)).all()
        }

        cash_balance = 0
        accounts_receivable = 0
        total_assets = 0
        total_liabilities = 0
        revenue = 0
        expenses = 0
        revenue_by_source = {}

        for account_id, account in gl_accounts.items():
            balance_data = account_balances.get(account_id, {"debit": 0, "credit": 0})
            debit = balance_data["debit"]
            credit = balance_data["credit"]

            if account.account_type == AccountType.ASSET:
                balance = debit - credit  # Assets normally debit
                total_assets += balance
                name_lower = account.account_name.lower()
                if "cash" in name_lower or "checking" in name_lower:
                    cash_balance += balance
                elif "receivable" in name_lower:
                    accounts_receivable += balance

            elif account.account_type == AccountType.LIABILITY:
                balance = credit - debit  # Liabilities normally credit
                total_liabilities += balance

            elif account.account_type == AccountType.REVENUE:
                balance = credit - debit  # Revenue normally credit (income)
                revenue += balance
                revenue_by_source[account.account_name] = balance

            elif account.account_type == AccountType.EXPENSE:
                expenses += debit - credit  # Expenses normally debit (costs)

        net_profit = revenue - expenses
        profit_margin = (net_profit / revenue * 100) if revenue > 0 else 0

        total_revenue = sum(revenue_by_source.values()) if revenue_by_source else 0
        revenue_breakdown = [
            {
                "name": name,
                "amount": float(amount),
                "percentage": round((amount / total_revenue * 100) if total_revenue > 0 else 0, 1),
            }
            for name, amount in sorted(revenue_by_source.items(), key=lambda x: x[1], reverse=True)
            if amount > 0
        ]

        return {
            "cash_balance": float(max(0, cash_balance)),
            "accounts_receivable": float(max(0, accounts_receivable)),
            "total_assets": float(max(0, total_assets)),
            "accounts_payable": float(max(0, total_liabilities)),  # simplified: all liabilities as accounts payable
            "net_profit": float(net_profit),
            "profit_margin": round(profit_margin, 1),
            "revenue_by_source": revenue_breakdown,
        }
    except Exception:
        logger.exception(f"Failed to compute financial snapshot for school {school_id}")
        return dict(_ZERO_SNAPSHOT)
