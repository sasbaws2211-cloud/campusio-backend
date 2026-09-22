"""Budget Service - per-account budgets for a fiscal period, and the
budget-vs-actual comparison report built from them.
"""
import logging
from typing import Optional, List
from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, and_, func

from datetime import datetime

from models.finance.budget import (
    Budget, BudgetCreate, BudgetUpdate, BudgetVsActualLine,
    BudgetPlan, BudgetPlanCreate, BudgetPlanUpdate,
)
from models.finance.chart_of_accounts import GLAccount
from models.finance.journal_entries import JournalEntry, JournalLineItem, PostingStatus
from models.finance.fiscal_period import FiscalPeriod
from models.school import School
from services.coa_service import CoaService
from services.fiscal_period_service import FiscalPeriodService

logger = logging.getLogger(__name__)


class BudgetError(Exception):
    """Base exception for budget service errors"""
    pass


class BudgetService:
    """Service for managing budgets and budget-vs-actual reporting"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.coa_service = CoaService(session)
        self.period_service = FiscalPeriodService(session)

    async def create_budget_line(
        self,
        school_id: str,
        budget_data: BudgetCreate,
        created_by: str,
    ) -> Budget:
        """Create a budget line for one account in one fiscal period

        Raises:
            BudgetError: If the period/account don't exist, or a budget line
                already exists for this (period, account) pair
        """
        period = await self.period_service.get_period_by_id(school_id, budget_data.fiscal_period_id)
        if not period:
            raise BudgetError(f"Fiscal period {budget_data.fiscal_period_id} not found")

        account = await self.coa_service.get_account_by_id(school_id, budget_data.gl_account_id)
        if not account:
            raise BudgetError(f"GL account {budget_data.gl_account_id} not found")

        existing = await self.session.execute(
            select(Budget).where(
                and_(
                    Budget.school_id == school_id,
                    Budget.fiscal_period_id == budget_data.fiscal_period_id,
                    Budget.gl_account_id == budget_data.gl_account_id,
                )
            )
        )
        if existing.scalar_one_or_none():
            raise BudgetError(
                f"A budget for account {account.account_code} already exists in this period "
                f"— update it instead of creating a duplicate"
            )

        if budget_data.budget_plan_id:
            plan = await self.session.execute(
                select(BudgetPlan).where(
                    and_(BudgetPlan.id == budget_data.budget_plan_id, BudgetPlan.school_id == school_id)
                )
            )
            if not plan.scalar_one_or_none():
                raise BudgetError(f"Budget plan {budget_data.budget_plan_id} not found")

        budget = Budget(
            school_id=school_id,
            fiscal_period_id=budget_data.fiscal_period_id,
            gl_account_id=budget_data.gl_account_id,
            budget_plan_id=budget_data.budget_plan_id,
            budgeted_amount=budget_data.budgeted_amount,
            notes=budget_data.notes,
            created_by=created_by,
        )
        self.session.add(budget)
        await self.session.commit()
        await self.session.refresh(budget)

        logger.info(
            f"Created budget for account {account.account_code} in period {period.period_name}: "
            f"{budget.budgeted_amount}"
        )
        return budget

    async def update_budget_line(
        self,
        school_id: str,
        budget_id: str,
        update_data: BudgetUpdate,
    ) -> Budget:
        result = await self.session.execute(
            select(Budget).where(and_(Budget.id == budget_id, Budget.school_id == school_id))
        )
        budget = result.scalar_one_or_none()
        if not budget:
            raise BudgetError(f"Budget {budget_id} not found")
        if budget.status not in ("draft", "rejected"):
            raise BudgetError(
                f"Cannot edit a budget in {budget.status} status — only draft or rejected budgets can be edited"
            )

        update_dict = update_data.model_dump(exclude_unset=True)
        for key, value in update_dict.items():
            setattr(budget, key, value)
        budget.updated_at = datetime.utcnow()

        self.session.add(budget)
        await self.session.commit()
        await self.session.refresh(budget)
        return budget

    async def requires_maker_checker(self, school_id: str) -> bool:
        """Whether this school has segregation-of-duties enabled (off by
        default) — mirrors ExpenseService/JournalEntryService's identically
        named method against the same School.require_maker_checker flag."""
        result = await self.session.execute(
            select(School.require_maker_checker).where(School.id == school_id)
        )
        return bool(result.scalar_one_or_none())

    async def submit_budget_line(self, school_id: str, budget_id: str, submitted_by: str) -> Budget:
        result = await self.session.execute(
            select(Budget).where(and_(Budget.id == budget_id, Budget.school_id == school_id))
        )
        budget = result.scalar_one_or_none()
        if not budget:
            raise BudgetError(f"Budget {budget_id} not found")
        if budget.status not in ("draft", "rejected"):
            raise BudgetError(
                f"Cannot submit budget in {budget.status} status (only draft or rejected can be submitted)"
            )

        budget.status = "submitted"
        budget.submitted_by = submitted_by
        budget.submitted_at = datetime.utcnow()
        budget.rejected_by = None
        budget.rejected_reason = None
        budget.updated_at = datetime.utcnow()
        self.session.add(budget)
        await self.session.commit()
        await self.session.refresh(budget)
        logger.info(f"Submitted budget {budget_id} for approval")
        return budget

    async def approve_budget_line(
        self, school_id: str, budget_id: str, approved_by: str, approval_notes: Optional[str] = None
    ) -> Budget:
        result = await self.session.execute(
            select(Budget).where(and_(Budget.id == budget_id, Budget.school_id == school_id))
        )
        budget = result.scalar_one_or_none()
        if not budget:
            raise BudgetError(f"Budget {budget_id} not found")
        if budget.status != "submitted":
            raise BudgetError(f"Cannot approve budget in {budget.status} status (only submitted can be approved)")
        if await self.requires_maker_checker(school_id) and budget.submitted_by == approved_by:
            raise BudgetError("Segregation of duties: you submitted this budget and cannot also approve it")

        budget.status = "approved"
        budget.approved_by = approved_by
        budget.approved_at = datetime.utcnow()
        budget.approval_notes = approval_notes
        budget.updated_at = datetime.utcnow()
        self.session.add(budget)
        await self.session.commit()
        await self.session.refresh(budget)
        logger.info(f"Approved budget {budget_id}")
        return budget

    async def reject_budget_line(
        self, school_id: str, budget_id: str, rejected_by: str, rejection_reason: str
    ) -> Budget:
        result = await self.session.execute(
            select(Budget).where(and_(Budget.id == budget_id, Budget.school_id == school_id))
        )
        budget = result.scalar_one_or_none()
        if not budget:
            raise BudgetError(f"Budget {budget_id} not found")
        if budget.status != "submitted":
            raise BudgetError(f"Cannot reject budget in {budget.status} status (only submitted can be rejected)")

        budget.status = "rejected"
        budget.rejected_by = rejected_by
        budget.rejected_reason = rejection_reason
        budget.updated_at = datetime.utcnow()
        self.session.add(budget)
        await self.session.commit()
        await self.session.refresh(budget)
        logger.info(f"Rejected budget {budget_id}: {rejection_reason}")
        return budget

    # --- Multi-year budget plans ---------------------------------------

    async def create_budget_plan(self, school_id: str, plan_data: BudgetPlanCreate, created_by: str) -> BudgetPlan:
        if plan_data.end_fiscal_year < plan_data.start_fiscal_year:
            raise BudgetError("end_fiscal_year cannot be before start_fiscal_year")

        plan = BudgetPlan(
            school_id=school_id,
            name=plan_data.name,
            start_fiscal_year=plan_data.start_fiscal_year,
            end_fiscal_year=plan_data.end_fiscal_year,
            notes=plan_data.notes,
            created_by=created_by,
        )
        self.session.add(plan)
        await self.session.commit()
        await self.session.refresh(plan)
        return plan

    async def list_budget_plans(self, school_id: str) -> List[BudgetPlan]:
        result = await self.session.execute(
            select(BudgetPlan).where(BudgetPlan.school_id == school_id).order_by(BudgetPlan.start_fiscal_year.desc())
        )
        return result.scalars().all()

    async def update_budget_plan(self, school_id: str, plan_id: str, update_data: BudgetPlanUpdate) -> BudgetPlan:
        result = await self.session.execute(
            select(BudgetPlan).where(and_(BudgetPlan.id == plan_id, BudgetPlan.school_id == school_id))
        )
        plan = result.scalar_one_or_none()
        if not plan:
            raise BudgetError(f"Budget plan {plan_id} not found")

        update_dict = update_data.model_dump(exclude_unset=True)
        for key, value in update_dict.items():
            setattr(plan, key, value)
        plan.updated_at = datetime.utcnow()
        self.session.add(plan)
        await self.session.commit()
        await self.session.refresh(plan)
        return plan

    async def delete_budget_plan(self, school_id: str, plan_id: str) -> None:
        result = await self.session.execute(
            select(BudgetPlan).where(and_(BudgetPlan.id == plan_id, BudgetPlan.school_id == school_id))
        )
        plan = result.scalar_one_or_none()
        if not plan:
            raise BudgetError(f"Budget plan {plan_id} not found")
        await self.session.delete(plan)
        await self.session.commit()

    async def delete_budget_line(self, school_id: str, budget_id: str) -> None:
        result = await self.session.execute(
            select(Budget).where(and_(Budget.id == budget_id, Budget.school_id == school_id))
        )
        budget = result.scalar_one_or_none()
        if not budget:
            raise BudgetError(f"Budget {budget_id} not found")
        if budget.status not in ("draft", "rejected"):
            raise BudgetError(
                f"Cannot delete a budget in {budget.status} status — only draft or rejected budgets can be deleted"
            )

        await self.session.delete(budget)
        await self.session.commit()

    async def list_budgets(self, school_id: str, fiscal_period_id: str) -> List[Budget]:
        result = await self.session.execute(
            select(Budget).where(
                and_(Budget.school_id == school_id, Budget.fiscal_period_id == fiscal_period_id)
            )
        )
        return result.scalars().all()

    async def get_budget_vs_actual(
        self,
        school_id: str,
        fiscal_period_id: str,
    ) -> List[BudgetVsActualLine]:
        """Compare each budgeted account's budget to its actual activity in the period

        Actual activity is summed from posted journal-entry line items dated
        within the period's [start_date, end_date] — the same period-bounded
        approach used for period close, not the account's running balance.
        """
        period = await self.period_service.get_period_by_id(school_id, fiscal_period_id)
        if not period:
            raise BudgetError(f"Fiscal period {fiscal_period_id} not found")

        budgets = await self.list_budgets(school_id, fiscal_period_id)
        if not budgets:
            return []

        lines: List[BudgetVsActualLine] = []
        for budget in budgets:
            account = await self.coa_service.get_account_by_id(school_id, budget.gl_account_id)
            if not account:
                continue

            result = await self.session.execute(
                select(
                    func.coalesce(func.sum(JournalLineItem.debit_amount), Decimal("0")).label("total_debit"),
                    func.coalesce(func.sum(JournalLineItem.credit_amount), Decimal("0")).label("total_credit"),
                )
                .join(JournalEntry, JournalLineItem.journal_entry_id == JournalEntry.id)
                .where(
                    and_(
                        JournalLineItem.gl_account_id == account.id,
                        JournalEntry.school_id == school_id,
                        # POSTED *and* REVERSED, not POSTED alone: reversing
                        # an entry flips its own status to REVERSED, so a
                        # POSTED-only filter would drop the original
                        # posting's line items while still counting the
                        # contra-entry's (which stays POSTED) — netting to
                        # the reversal amount instead of zero. Only DRAFT/
                        # REJECTED entries never actually hit the ledger.
                        JournalEntry.posting_status.in_([PostingStatus.POSTED, PostingStatus.REVERSED]),
                        JournalEntry.entry_date >= period.start_date,
                        JournalEntry.entry_date <= period.end_date,
                    )
                )
            )
            row = result.first()
            total_debit = row.total_debit or Decimal("0")
            total_credit = row.total_credit or Decimal("0")

            actual = (total_debit - total_credit) if account.normal_balance == "debit" else (total_credit - total_debit)
            variance = actual - budget.budgeted_amount
            variance_pct = (
                float(variance / budget.budgeted_amount * 100) if budget.budgeted_amount != 0 else None
            )

            lines.append(
                BudgetVsActualLine(
                    gl_account_id=account.id,
                    account_code=account.account_code,
                    account_name=account.account_name,
                    budgeted_amount=budget.budgeted_amount,
                    actual_amount=actual,
                    variance=variance,
                    variance_percentage=round(variance_pct, 1) if variance_pct is not None else None,
                )
            )

        return sorted(lines, key=lambda l: l.account_code)

    async def check_budget_available(
        self, school_id: str, gl_account_id: str, as_of_date, additional_amount: Decimal,
    ) -> Optional[dict]:
        """Would posting `additional_amount` more against this account push
        it past its budgeted amount for the fiscal period covering
        `as_of_date`? Returns None if there's no budget line for this
        (account, period) combination at all — nothing to check against,
        same as before this existed. Previously budget numbers were ONLY
        ever consulted by get_budget_vs_actual's after-the-fact report;
        nothing in the expense-approval path ever called this, so a school
        could blow through an approved budget with zero warning anywhere."""
        period = await self.period_service.get_period_by_date(school_id, as_of_date)
        if not period:
            return None

        budget_result = await self.session.execute(
            select(Budget).where(
                Budget.school_id == school_id, Budget.gl_account_id == gl_account_id, Budget.fiscal_period_id == period.id,
            )
        )
        budget = budget_result.scalar_one_or_none()
        if not budget:
            return None

        account = await self.coa_service.get_account_by_id(school_id, gl_account_id)
        if not account:
            return None

        result = await self.session.execute(
            select(
                func.coalesce(func.sum(JournalLineItem.debit_amount), Decimal("0")).label("total_debit"),
                func.coalesce(func.sum(JournalLineItem.credit_amount), Decimal("0")).label("total_credit"),
            )
            .join(JournalEntry, JournalLineItem.journal_entry_id == JournalEntry.id)
            .where(
                and_(
                    JournalLineItem.gl_account_id == account.id,
                    JournalEntry.school_id == school_id,
                    JournalEntry.posting_status.in_([PostingStatus.POSTED, PostingStatus.REVERSED]),
                    JournalEntry.entry_date >= period.start_date,
                    JournalEntry.entry_date <= period.end_date,
                )
            )
        )
        row = result.first()
        total_debit = row.total_debit or Decimal("0")
        total_credit = row.total_credit or Decimal("0")
        actual_so_far = (total_debit - total_credit) if account.normal_balance == "debit" else (total_credit - total_debit)

        projected = actual_so_far + additional_amount
        exceeds_by = projected - budget.budgeted_amount
        return {
            "fiscal_period_id": period.id,
            "gl_account_id": account.id,
            "account_code": account.account_code,
            "budgeted_amount": budget.budgeted_amount,
            "actual_so_far": actual_so_far,
            "projected_after": projected,
            "exceeds_budget": exceeds_by > 0,
            "exceeds_by": exceeds_by if exceeds_by > 0 else Decimal("0"),
        }

    async def get_budget_plan_vs_actual(self, school_id: str, plan_id: str) -> List[BudgetVsActualLine]:
        """Sum budgeted-vs-actual across every fiscal period this plan's
        budget lines span. Reuses the exact same period-bounded actual
        query as get_budget_vs_actual per period, then aggregates by
        account across periods — no change needed to the single-period
        method or its callers.
        """
        plan_result = await self.session.execute(
            select(BudgetPlan).where(and_(BudgetPlan.id == plan_id, BudgetPlan.school_id == school_id))
        )
        plan = plan_result.scalar_one_or_none()
        if not plan:
            raise BudgetError(f"Budget plan {plan_id} not found")

        budgets = (
            await self.session.execute(
                select(Budget).where(and_(Budget.school_id == school_id, Budget.budget_plan_id == plan_id))
            )
        ).scalars().all()
        if not budgets:
            return []

        period_ids = {b.fiscal_period_id for b in budgets}
        periods: dict[str, FiscalPeriod] = {}
        for pid in period_ids:
            period = await self.period_service.get_period_by_id(school_id, pid)
            if period:
                periods[pid] = period

        totals: dict = {}  # gl_account_id -> {"account": GLAccount, "budgeted": Decimal, "actual": Decimal}
        for budget in budgets:
            period = periods.get(budget.fiscal_period_id)
            if not period:
                continue
            account = await self.coa_service.get_account_by_id(school_id, budget.gl_account_id)
            if not account:
                continue

            result = await self.session.execute(
                select(
                    func.coalesce(func.sum(JournalLineItem.debit_amount), Decimal("0")).label("total_debit"),
                    func.coalesce(func.sum(JournalLineItem.credit_amount), Decimal("0")).label("total_credit"),
                )
                .join(JournalEntry, JournalLineItem.journal_entry_id == JournalEntry.id)
                .where(
                    and_(
                        JournalLineItem.gl_account_id == account.id,
                        JournalEntry.school_id == school_id,
                        JournalEntry.posting_status.in_([PostingStatus.POSTED, PostingStatus.REVERSED]),
                        JournalEntry.entry_date >= period.start_date,
                        JournalEntry.entry_date <= period.end_date,
                    )
                )
            )
            row = result.first()
            total_debit = row.total_debit or Decimal("0")
            total_credit = row.total_credit or Decimal("0")
            actual = (total_debit - total_credit) if account.normal_balance == "debit" else (total_credit - total_debit)

            bucket = totals.setdefault(account.id, {"account": account, "budgeted": Decimal("0"), "actual": Decimal("0")})
            bucket["budgeted"] += budget.budgeted_amount
            bucket["actual"] += actual

        lines: List[BudgetVsActualLine] = []
        for bucket in totals.values():
            account = bucket["account"]
            budgeted = bucket["budgeted"]
            actual = bucket["actual"]
            variance = actual - budgeted
            variance_pct = float(variance / budgeted * 100) if budgeted != 0 else None
            lines.append(
                BudgetVsActualLine(
                    gl_account_id=account.id,
                    account_code=account.account_code,
                    account_name=account.account_name,
                    budgeted_amount=budgeted,
                    actual_amount=actual,
                    variance=variance,
                    variance_percentage=round(variance_pct, 1) if variance_pct is not None else None,
                )
            )

        return sorted(lines, key=lambda l: l.account_code)
