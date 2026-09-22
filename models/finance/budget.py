"""Budget models — per-account budgeted amounts for a fiscal period, and the
budget-vs-actual comparison built from them.
"""
from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime
from decimal import Decimal
import uuid

from .chart_of_accounts import MONEY_MAX_DIGITS, MONEY_DECIMAL_PLACES, Money


class Budget(SQLModel, table=True):
    """A single budget line: one GL account's budgeted amount for one fiscal period"""
    __tablename__ = "budgets"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)

    fiscal_period_id: str = Field(index=True)
    gl_account_id: str = Field(index=True)
    # Nullable link to a BudgetPlan wrapping several period-scoped budget
    # lines into one multi-year plan — a plain budget line with no plan is
    # unaffected, this is purely additive grouping.
    budget_plan_id: Optional[str] = Field(default=None, index=True)

    budgeted_amount: Decimal = Field(max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES)
    notes: Optional[str] = None

    # Approval workflow — plain str, not a native Postgres enum (this
    # codebase's settled convention for any status column added after the
    # original baseline migration). draft -> submitted -> approved/rejected.
    status: str = Field(default="draft", index=True)
    submitted_by: Optional[str] = None
    submitted_at: Optional[datetime] = None
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    approval_notes: Optional[str] = None
    rejected_by: Optional[str] = None
    rejected_reason: Optional[str] = None

    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class BudgetCreate(SQLModel):
    """Validation model for creating a budget line"""
    fiscal_period_id: str
    gl_account_id: str
    budgeted_amount: Decimal
    notes: Optional[str] = None
    budget_plan_id: Optional[str] = None


class BudgetUpdate(SQLModel):
    """Validation model for updating a budget line"""
    budgeted_amount: Optional[Decimal] = None
    notes: Optional[str] = None


class BudgetApprovalRequest(SQLModel):
    approval_notes: Optional[str] = None


class BudgetRejectionRequest(SQLModel):
    rejection_reason: str


class BudgetResponse(SQLModel):
    """Response model for a budget line"""
    id: str
    school_id: str
    fiscal_period_id: str
    gl_account_id: str
    budget_plan_id: Optional[str]
    budgeted_amount: Money
    notes: Optional[str]
    status: str
    submitted_by: Optional[str]
    submitted_at: Optional[datetime]
    approved_by: Optional[str]
    approved_at: Optional[datetime]
    approval_notes: Optional[str]
    rejected_by: Optional[str]
    rejected_reason: Optional[str]
    created_by: str
    created_at: datetime
    updated_at: datetime


class BudgetPlan(SQLModel, table=True):
    """A multi-year budget plan wrapping several period-scoped Budget lines
    (one per fiscal_period_id x gl_account_id, exactly as before) under one
    umbrella — additive, existing single-period budgets are unaffected."""
    __tablename__ = "budget_plans"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    name: str
    start_fiscal_year: int = Field(index=True)
    end_fiscal_year: int = Field(index=True)
    status: str = Field(default="draft")  # draft | active | closed
    notes: Optional[str] = None

    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class BudgetPlanCreate(SQLModel):
    name: str
    start_fiscal_year: int
    end_fiscal_year: int
    notes: Optional[str] = None


class BudgetPlanUpdate(SQLModel):
    name: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class BudgetPlanResponse(SQLModel):
    id: str
    school_id: str
    name: str
    start_fiscal_year: int
    end_fiscal_year: int
    status: str
    notes: Optional[str]
    created_by: str
    created_at: datetime
    updated_at: datetime


class BudgetVsActualLine(SQLModel):
    """One row of a budget-vs-actual report"""
    gl_account_id: str
    account_code: str
    account_name: str
    budgeted_amount: Money
    actual_amount: Money
    variance: Money  # actual - budgeted (positive = over budget for expenses, under for revenue)
    variance_percentage: Optional[float] = None
