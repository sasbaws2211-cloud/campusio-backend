"""Staff loan / salary advance models

Payroll already had unused PayrollCategory.DEDUCTION_LOAN / DEDUCTION_ADVANCE
placeholders (models/payroll.py) with nothing behind them: no way to record a
loan's principal, track how much has been repaid, or know when it's fully
settled. These models add that ledger; repayments are applied to a payroll
run as PayrollAdjustment rows (see StaffLoanService.apply_loan_repayment),
reusing the existing net-pay/GL posting machinery instead of duplicating it.
"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Column
from sqlalchemy.types import Enum as SQLEnum
from typing import Optional
from datetime import datetime
from enum import Enum
import uuid


class LoanStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class StaffLoan(SQLModel, table=True):
    """A staff loan or salary advance, repaid via fixed installment
    deductions from payroll runs."""
    __tablename__ = "staff_loans"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    staff_id: str = Field(index=True)

    loan_type: str = Field(default="loan")  # "loan" or "advance"
    principal_amount: float
    installment_amount: float  # fixed deduction per payroll run
    total_installments: int
    installments_paid: int = Field(default=0)
    outstanding_balance: float = Field(default=0.0)  # set to principal_amount on approval

    # Stored as plain varchar (native_enum=False), not a native Postgres
    # enum — matches Staff.payout_verification_status's reasoning: sidesteps
    # needing an ALTER TYPE migration for every future status added here.
    status: LoanStatus = Field(
        default=LoanStatus.PENDING,
        sa_column=Column(
            SQLEnum('pending', 'approved', 'rejected', 'active', 'completed', 'cancelled', name='loanstatus', native_enum=False)
        ),
    )
    reason: Optional[str] = None

    # Which payroll period the first deduction should be taken from.
    start_period_year: int
    start_period_month: int

    requested_by: Optional[str] = None
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    disbursed_at: Optional[datetime] = None

    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class LoanWriteOffRequest(SQLModel):
    reason: str


class StaffLoanCreate(SQLModel):
    staff_id: str
    loan_type: str = "loan"
    principal_amount: float
    installment_amount: float
    total_installments: int
    reason: Optional[str] = None
    start_period_year: int
    start_period_month: int
    notes: Optional[str] = None


class StaffLoanRepayment(SQLModel, table=True):
    """One row per payroll run a loan installment was deducted on — audit
    trail for StaffLoan.outstanding_balance/installments_paid."""
    __tablename__ = "staff_loan_repayments"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    loan_id: str = Field(index=True)
    # Optional: a normal installment (see StaffLoanService.apply_loan_repayment)
    # always sets both of these. A full-balance payoff at staff exit
    # (settle_loan_at_exit) has neither — it isn't deducted from any
    # specific payroll run, so staff_exit_id is set instead.
    payroll_run_id: Optional[str] = Field(default=None, index=True)
    payroll_adjustment_id: Optional[str] = Field(default=None, index=True)
    staff_exit_id: Optional[str] = Field(default=None, index=True)
    staff_id: str = Field(index=True)

    installment_number: int
    amount_paid: float
    balance_after: float

    created_at: datetime = Field(default_factory=datetime.utcnow)
