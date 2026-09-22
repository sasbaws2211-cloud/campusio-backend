"""Payroll models for salary management and processing"""
from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime
from enum import Enum
import uuid


class PaySchedule(str, Enum):
    """Payment schedule frequency"""
    MONTHLY = "monthly"
    BIWEEKLY = "biweekly"
    WEEKLY = "weekly"


class PayrollStatus(str, Enum):
    """Status of payroll run"""
    DRAFT = "draft"
    GENERATED = "generated"
    APPROVED = "approved"
    POSTED = "posted"
    REJECTED = "rejected"
    # A POSTED run discovered to be wrong before any staff member has
    # actually been paid — see PayrollService.void_payroll_run. Reverses
    # the GL entry and stops here; once disbursement has started for any
    # line item, voiding is refused (money already moved).
    VOIDED = "voided"


class PayrollCategory(str, Enum):
    """Categories for payroll line items"""
    BASIC_SALARY = "basic_salary"
    ALLOWANCE_HOUSING = "allowance_housing"
    ALLOWANCE_TRANSPORT = "allowance_transport"
    ALLOWANCE_MEALS = "allowance_meals"
    ALLOWANCE_UTILITIES = "allowance_utilities"
    ALLOWANCE_OTHER = "allowance_other"
    TAX_INCOME = "tax_income"
    DEDUCTION_PENSION = "deduction_pension"
    DEDUCTION_NSSF = "deduction_nssf"
    DEDUCTION_LOAN = "deduction_loan"
    DEDUCTION_ADVANCE = "deduction_advance"
    DEDUCTION_OTHER = "deduction_other"


class SalaryGrade(SQLModel, table=True):
    """A school's own reference salary structure (grade + step -> basic
    salary), e.g. "Teacher Grade II, Step 3 -> GHS 2,400". Previously every
    PayrollContract.basic_salary was independently typed in with no shared
    structure a school could use to keep pay consistent/equitable across
    staff on the same grade, or to see at a glance what a grade's current
    rate is. Purely a reference table: creating a contract from a grade
    copies that grade's amount into PayrollContract.basic_salary at that
    moment (see PayrollContract.salary_grade_id) — editing a grade later
    never silently changes any existing contract's actual pay."""
    __tablename__ = "payroll_salary_grades"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    grade_name: str  # e.g. "Teacher Grade II"
    step: int = Field(default=1)  # e.g. 1-5 within a grade, for annual increments
    basic_salary: float
    description: Optional[str] = None
    is_active: bool = Field(default=True, index=True)
    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class SalaryGradeCreate(SQLModel):
    grade_name: str
    step: int = 1
    basic_salary: float
    description: Optional[str] = None


class SalaryGradeUpdate(SQLModel):
    grade_name: Optional[str] = None
    step: Optional[int] = None
    basic_salary: Optional[float] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class PayrollContract(SQLModel, table=True):
    """Staff payroll contract - defines salary and deduction rules"""
    __tablename__ = "payroll_contracts"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    staff_id: str = Field(index=True)
    basic_salary: float
    pay_schedule: PaySchedule = PaySchedule.MONTHLY
    currency: str = Field(default="GHS")
    
    # Allowances (all optional)
    allowance_housing: float = Field(default=0.0)
    allowance_transport: float = Field(default=0.0)
    allowance_meals: float = Field(default=0.0)
    allowance_utilities: float = Field(default=0.0)
    allowance_other: float = Field(default=0.0)
    allowance_other_description: Optional[str] = None
    
    # Deductions (all optional)
    tax_rate_percent: float = Field(default=0.0)  # Income tax percentage — used when tax_calculation_mode="flat"
    # "flat" (default — preserves every existing contract's exact behavior,
    # a single rate HR enters and trusts) or "bracket" — compute income tax
    # as a real progressive calculation against this school's own
    # PayeBracket table instead of one flat rate applied to the whole
    # gross amount. See services/payroll_service.py::calculate_paye_from_brackets.
    tax_calculation_mode: str = Field(default="flat")
    pension_rate_percent: float = Field(default=0.0)  # Pension contribution %
    # Employee-side SSNIT Tier-1 rate — applied to BASIC salary (not gross;
    # SSNIT contributions are statutorily computed on basic pay only). A
    # school configures its own rate here rather than this codebase
    # hardcoding Ghana's 5.5%, matching the same "starting point, not
    # gospel" honesty as the PAYE bracket seed above.
    nssf_rate_percent: float = Field(default=0.0)  # NSSF (employee) contribution %
    # Employer-side SSNIT Tier-1 share — a real cost to the school, not a
    # deduction from staff pay, and previously never tracked or GL-posted
    # anywhere (services/statutory_export_service.py estimated it on the
    # fly purely for a CSV export). Statutory total employer SSNIT is ~13%
    # of basic, split into an ~8% Tier-1 share (this field) and a 5%
    # Tier-2 share (nssf_tier2_rate_percent below, remitted to a separate
    # private pension trustee, not SSNIT itself).
    employer_nssf_rate_percent: float = Field(default=8.0)
    # Ghana's mandatory Tier-2 occupational pension — 5% of basic,
    # entirely employer-funded, remitted to a licensed private trustee
    # rather than SSNIT. Previously not modeled at all: only a single
    # combined "NSSF" rate existed, which is Tier-1 only.
    nssf_tier2_rate_percent: float = Field(default=5.0)
    other_deduction: float = Field(default=0.0)
    other_deduction_description: Optional[str] = None
    # Divisor used to derive an hourly rate from basic_salary for overtime
    # pay (routers/hr_overtime.py::push_overtime_to_payroll) — previously
    # hardcoded to 160 (40h/week x 4 weeks) for every contract regardless
    # of that staff member's actual standard hours.
    standard_monthly_hours: float = Field(default=160.0)
    # Arbitrary extra allowance/deduction line items beyond the 5 fixed
    # allowance categories above — a JSON list of {"name": str, "amount":
    # float} objects. Previously a school with e.g. a "responsibility
    # allowance" or "risk allowance" had nowhere to put it except
    # shoehorning it into allowance_other (a single flat amount with no
    # per-item description). Folded into total_allowances by
    # PayrollService.calculate_allowances; each item shown individually in
    # the payslip breakdown.
    extra_allowances: Optional[str] = None
    # Same idea as extra_allowances but for deductions beyond the fixed
    # tax/pension/nssf/other_deduction fields — a JSON list of {"name":
    # str, "amount": float} objects, e.g. a union dues or uniform-cost
    # deduction specific to one staff member.
    extra_deductions: Optional[str] = None
    # A shared salary grade/step this contract's basic_salary was derived
    # from (see SalaryGrade below) — purely a reference for consistency
    # reporting; basic_salary itself is still the number actually used in
    # payroll calculations, so re-grading a SalaryGrade later never
    # silently changes an existing contract's pay.
    salary_grade_id: Optional[str] = None

    # Contract dates
    effective_from: datetime = Field(index=True)
    effective_to: Optional[datetime] = None
    
    # Status
    is_active: bool = Field(default=True, index=True)
    
    # Audit fields
    created_by: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class PayrollContractCreate(SQLModel):
    """Validation model for creating payroll contracts"""
    staff_id: str
    basic_salary: float
    pay_schedule: PaySchedule = PaySchedule.MONTHLY
    currency: str = "GHS"
    allowance_housing: float = 0.0
    allowance_transport: float = 0.0
    allowance_meals: float = 0.0
    allowance_utilities: float = 0.0
    allowance_other: float = 0.0
    allowance_other_description: Optional[str] = None
    tax_rate_percent: float = 0.0
    tax_calculation_mode: str = "flat"
    pension_rate_percent: float = 0.0
    nssf_rate_percent: float = 0.0
    employer_nssf_rate_percent: float = 8.0
    nssf_tier2_rate_percent: float = 5.0
    other_deduction: float = 0.0
    other_deduction_description: Optional[str] = None
    standard_monthly_hours: float = 160.0
    extra_allowances: Optional[str] = None
    extra_deductions: Optional[str] = None
    effective_from: datetime
    effective_to: Optional[datetime] = None
    notes: Optional[str] = None
    # A shared salary grade/step this contract's basic_salary was derived
    # from (see SalaryGrade below) — purely a reference for consistency
    # reporting; basic_salary itself is still the number actually used in
    # payroll calculations, so re-grading a SalaryGrade later never
    # silently changes an existing contract's pay.
    salary_grade_id: Optional[str] = None


class PayrollContractUpdate(SQLModel):
    """Validation model for updating payroll contracts"""
    basic_salary: Optional[float] = None
    pay_schedule: Optional[PaySchedule] = None
    allowance_housing: Optional[float] = None
    allowance_transport: Optional[float] = None
    allowance_meals: Optional[float] = None
    allowance_utilities: Optional[float] = None
    allowance_other: Optional[float] = None
    allowance_other_description: Optional[str] = None
    tax_rate_percent: Optional[float] = None
    tax_calculation_mode: Optional[str] = None
    pension_rate_percent: Optional[float] = None
    nssf_rate_percent: Optional[float] = None
    employer_nssf_rate_percent: Optional[float] = None
    nssf_tier2_rate_percent: Optional[float] = None
    other_deduction: Optional[float] = None
    other_deduction_description: Optional[str] = None
    standard_monthly_hours: Optional[float] = None
    extra_allowances: Optional[str] = None
    extra_deductions: Optional[str] = None
    salary_grade_id: Optional[str] = None
    effective_to: Optional[datetime] = None
    is_active: Optional[bool] = None
    notes: Optional[str] = None


class PayeBracket(SQLModel, table=True):
    """A school's own progressive income-tax bands — used when a
    PayrollContract has tax_calculation_mode="bracket" instead of a single
    flat tax_rate_percent. Versioned by year since bands change; a school
    seeds a starting point (services.payroll_service.seed_default_paye_brackets,
    illustrative Ghana GRA-style monthly bands, same "starting point the
    school must confirm" honesty as RulePresetService's deduction-rule
    presets elsewhere in this codebase) and edits it themselves — this
    codebase does not claim to track live statutory changes."""
    __tablename__ = "payroll_paye_brackets"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    year: int = Field(index=True)
    lower_bound: float  # monthly taxable income lower bound, inclusive
    upper_bound: Optional[float] = None  # None = no upper bound (top/final bracket)
    rate_percent: float  # marginal rate applied to income within this band only
    sort_order: int = Field(default=0)
    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class PayeBracketCreate(SQLModel):
    year: int
    lower_bound: float
    upper_bound: Optional[float] = None
    rate_percent: float
    sort_order: int = 0


class PayeBracketUpdate(SQLModel):
    lower_bound: Optional[float] = None
    upper_bound: Optional[float] = None
    rate_percent: Optional[float] = None
    sort_order: Optional[int] = None


class PayrollRun(SQLModel, table=True):
    """Monthly/periodic payroll run - aggregates all payroll for a period"""
    __tablename__ = "payroll_runs"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    # None = school-wide (every campus) — the only mode that existed
    # before. Set = this run covers only staff assigned to that campus,
    # for a school where a campus-scoped admin needs to run payroll for
    # just their own campus without touching everyone else's.
    campus_id: Optional[str] = Field(default=None, index=True)
    period_year: int = Field(index=True)
    period_month: int = Field(index=True)
    period_name: str = Field(default="")  # e.g., "January 2026"

    # Which contracts this run covers — a school can run monthly payroll for
    # most staff and a separate weekly/biweekly run in the same calendar
    # month for others. Distinct runs per schedule, not one run mixing both.
    pay_schedule: PaySchedule = Field(default=PaySchedule.MONTHLY, index=True)

    status: PayrollStatus = Field(default=PayrollStatus.DRAFT, index=True)
    
    # Totals
    total_gross: float = Field(default=0.0)
    total_allowances: float = Field(default=0.0)
    total_deductions: float = Field(default=0.0)
    total_net: float = Field(default=0.0)
    
    # Staff count
    staff_count: int = Field(default=0)
    
    # Audit fields
    generated_by: Optional[str] = None
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    posted_at: Optional[datetime] = None
    notes: Optional[str] = None

    # The GL journal entry created when this run was posted (Dr Salaries
    # Expense / Cr Salaries+NSSF+Pension+Tax Payable) — previously computed
    # and returned to the caller but never actually stored on the row
    # itself, so nothing could later reverse it (see void_payroll_run) or
    # look it up without re-deriving it from ReferenceType.PAYROLL_RUN.
    journal_entry_id: Optional[str] = None
    # The GL entry clearing Salaries Payable as staff are actually paid via
    # disburse_payroll_run (Dr Salaries Payable / Cr Bank) — a run can be
    # disbursed more than once (retrying failed line items), so this is the
    # id of the LATEST clearing entry, not necessarily the only one.
    disbursement_journal_entry_id: Optional[str] = None
    voided_at: Optional[datetime] = None
    voided_by: Optional[str] = None
    void_reason: Optional[str] = None
    reversal_journal_entry_id: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class PayrollRunCreate(SQLModel):
    """Validation model for creating payroll runs"""
    period_year: int
    period_month: int
    pay_schedule: PaySchedule = PaySchedule.MONTHLY
    notes: Optional[str] = None
    # None = school-wide (every campus). A campus-scoped caller's own
    # campus always overrides this (see dependencies.resolve_write_campus_id).
    campus_id: Optional[str] = None


class PayrollRunActionRequest(SQLModel):
    """Request body for approving/rejecting a payroll run"""
    notes: Optional[str] = None


class PayrollLineItem(SQLModel, table=True):
    """Individual staff payroll for a payroll run"""
    __tablename__ = "payroll_line_items"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    payroll_run_id: str = Field(index=True)
    school_id: str = Field(index=True)
    staff_id: str = Field(index=True)
    
    # Calculation components
    basic_salary: float
    total_allowances: float
    gross_amount: float
    
    # Deductions breakdown
    tax_amount: float = Field(default=0.0)
    pension_amount: float = Field(default=0.0)
    nssf_amount: float = Field(default=0.0)  # Employee-side SSNIT Tier-1, deducted from pay
    other_deductions: float = Field(default=0.0)

    # Employer-side statutory contributions — NOT deducted from this staff
    # member's pay (they're the school's own cost), tracked here purely so
    # the real employer liability can be GL-posted and exported instead of
    # only ever existing as an on-the-fly CSV estimate.
    employer_nssf_amount: float = Field(default=0.0)  # Employer SSNIT Tier-1 share
    nssf_tier2_amount: float = Field(default=0.0)  # Mandatory Tier-2, employer-funded
    
    total_deductions: float
    net_amount: float  # Final take-home: gross - total_deductions + total_adjustments, floored at 0

    # Sum of APPROVED PayrollAdjustment rows for this staff member on this
    # run. Unapproved adjustments don't affect net_amount at all.
    total_adjustments: float = Field(default=0.0)

    # JSON field for detailed breakdown (flexibility for future)
    breakdown: Optional[str] = None  # JSON string with itemized breakdown

    # Disbursement — "posted" only books the GL liability; these track
    # whether the staff member has actually been paid via a real Paystack
    # transfer to their verified bank/mobile money account.
    payment_status: str = Field(default="unpaid")  # unpaid, paid, failed
    paid_at: Optional[datetime] = None
    transfer_reference: Optional[str] = None
    payment_failure_reason: Optional[str] = None

    # Audit fields
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class PayrollAdjustment(SQLModel, table=True):
    """Manual adjustments to payroll (bonus, penalty, refund, etc.)"""
    __tablename__ = "payroll_adjustments"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    payroll_run_id: str = Field(index=True)
    school_id: str = Field(index=True)
    staff_id: str = Field(index=True)
    
    adjustment_type: str  # "bonus", "penalty", "refund", "advance_recovery", etc.
    amount: float
    reason: str
    
    # Audit
    created_by: Optional[str] = None
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class PayrollAdjustmentCreate(SQLModel):
    """Validation model for creating adjustments.

    amount is signed: positive for bonus-style additions, negative for
    penalty/advance-recovery-style deductions from net pay.
    """
    payroll_run_id: str
    staff_id: str
    adjustment_type: str
    amount: float
    reason: str


class PayslipResponse(SQLModel):
    """Response model for payslip view"""
    payroll_run_id: str
    period_name: str
    staff_id: str
    staff_name: str
    
    basic_salary: float
    total_allowances: float
    gross_amount: float
    
    tax_amount: float
    pension_amount: float
    nssf_amount: float
    other_deductions: float
    total_deductions: float
    
    net_amount: float
    currency: str
    
    generated_at: datetime
    posted_at: Optional[datetime] = None


# ==================== Deduction Rules Models ====================

class RuleOperator(str, Enum):
    """Operators for rule conditions"""
    EQUALS = "equals"
    GREATER_THAN = "greater_than"
    LESS_THAN = "less_than"
    GREATER_EQUAL = "greater_equal"
    LESS_EQUAL = "less_equal"
    BETWEEN = "between"
    CONTAINS = "contains"


class RuleType(str, Enum):
    """Types of deduction rules"""
    SALARY_BRACKET = "salary_bracket"  # Deduction based on salary range
    YEARS_SERVICE = "years_service"    # Deduction based on tenure
    ATTENDANCE = "attendance"          # Deduction based on attendance
    CUSTOM = "custom"                  # Custom rule with expression


class DeductionRule(SQLModel, table=True):
    """Automatic deduction rules for payroll processing"""
    __tablename__ = "deduction_rules"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    
    # Basic info
    name: str = Field(index=True)  # e.g., "Pension Bracket A"
    description: Optional[str] = None
    rule_type: RuleType = Field(index=True)
    
    # Rule conditions
    operator: RuleOperator  # How to evaluate the condition
    condition_field: str    # What field to check (e.g., "basic_salary", "years_service", "absent_days")
    condition_value_min: Optional[float] = None  # Lower bound or single value
    condition_value_max: Optional[float] = None  # Upper bound (for BETWEEN)
    
    # Deduction details
    deduction_type: str  # "percentage" or "fixed"
    deduction_amount: float  # Percentage (0-100) or fixed GHS amount
    deduction_category: str = "other"  # Category name for reporting
    deduction_description: Optional[str] = None
    
    # Rules logic
    priority: int = Field(default=0, index=True)  # Execution order (higher = earlier)
    is_active: bool = Field(default=True, index=True)
    
    # Optional conditional expression for complex rules
    # Example: "basic_salary > 5000 and years_service > 5"
    expression: Optional[str] = None  # JSON-serializable condition tree
    
    # Audit
    created_by: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class DeductionRuleCreate(SQLModel):
    """Validation model for creating deduction rules"""
    name: str
    description: Optional[str] = None
    rule_type: RuleType
    
    operator: RuleOperator
    condition_field: str
    condition_value_min: Optional[float] = None
    condition_value_max: Optional[float] = None
    
    deduction_type: str  # "percentage" or "fixed"
    deduction_amount: float
    deduction_category: str = "other"
    deduction_description: Optional[str] = None
    
    priority: int = 0
    expression: Optional[str] = None
    notes: Optional[str] = None


class DeductionRuleUpdate(SQLModel):
    """Validation model for updating deduction rules"""
    name: Optional[str] = None
    description: Optional[str] = None
    operator: Optional[RuleOperator] = None
    condition_field: Optional[str] = None
    condition_value_min: Optional[float] = None
    condition_value_max: Optional[float] = None
    deduction_type: Optional[str] = None
    deduction_amount: Optional[float] = None
    deduction_category: Optional[str] = None
    deduction_description: Optional[str] = None
    priority: Optional[int] = None
    is_active: Optional[bool] = None
    expression: Optional[str] = None
    notes: Optional[str] = None


class DeductionRuleResponse(SQLModel):
    """Response model for deduction rules"""
    id: str
    school_id: str
    name: str
    description: Optional[str]
    rule_type: RuleType
    operator: RuleOperator
    condition_field: str
    condition_value_min: Optional[float]
    condition_value_max: Optional[float]
    deduction_type: str
    deduction_amount: float
    deduction_category: str
    priority: int
    is_active: bool
    created_at: datetime


class RuleEvaluationResult(SQLModel):
    """Result of evaluating a rule against staff data"""
    rule_id: str
    rule_name: str
    matched: bool
    deduction_amount: float
    deduction_type: str
    reason: Optional[str] = None
