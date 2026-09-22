"""Expense models for school operational spending

Tracks all school expenses (utilities, supplies, maintenance, etc.)
with approval workflow and GL account mapping.
"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Enum as SQLEnum, Column
from typing import Optional, List
from datetime import datetime
from decimal import Decimal
from enum import Enum
import uuid

from .chart_of_accounts import MONEY_MAX_DIGITS, MONEY_DECIMAL_PLACES, Money


class ExpenseCategory(str, Enum):
    """Categories of school expenses"""
    UTILITIES = "utilities"              # Electricity, water, internet, phone
    SUPPLIES = "supplies"                # Office, classroom, lab materials
    MAINTENANCE = "maintenance"          # Building repairs, equipment maintenance
    TRANSPORTATION = "transportation"    # Transport for school activities
    MEALS = "meals"                      # Staff meals, student programs
    PROFESSIONAL_SERVICES = "professional_services"  # Consultants, auditors
    INSURANCE = "insurance"              # School insurance policies
    EQUIPMENT = "equipment"              # Office/teaching equipment purchases
    FURNITURE = "furniture"              # Desks, chairs, filing cabinets
    CLEANING = "cleaning"                # Cleaning supplies and services
    SECURITY = "security"                # Security services and equipment
    PROGRAMS = "programs"                # Educational programs, workshops
    TRAVEL = "travel"                    # Staff travel, conferences
    PRINTING = "printing"                # Printing and stationery
    MISCELLANEOUS = "miscellaneous"      # Other expenses


class ExpenseStatus(str, Enum):
    """Status of an expense record"""
    DRAFT = "draft"                  # Created but not submitted
    PENDING = "pending"              # Submitted, awaiting approval
    APPROVED = "approved"            # Approved by admin
    REJECTED = "rejected"            # Rejected and not posted
    POSTED = "posted"                # Posted to GL


class PaymentStatus(str, Enum):
    """Payment status of an expense"""
    OUTSTANDING = "outstanding"     # Not yet paid
    PARTIAL = "partial"             # Partially paid
    PAID = "paid"                   # Fully paid


class Vendor(SQLModel, table=True):
    """A minimal supplier/vendor master record — previously `Expense.vendor_name`
    was the only place a vendor existed anywhere in this codebase (a free
    string, re-typed differently every time), with no way to see "everything
    we've ever bought from this supplier" or track payment terms. Expense
    keeps `vendor_name` for backward compatibility and for a one-off payee
    that doesn't warrant a master record; `vendor_id` is the real link when
    one exists."""
    __tablename__ = "vendors"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    name: str
    contact_name: Optional[str] = None
    contact_phone: Optional[str] = None
    contact_email: Optional[str] = None
    address: Optional[str] = None
    # How many days after an invoice this vendor expects payment — purely
    # informational today (not yet enforced anywhere), but gives a school
    # somewhere real to record it instead of a side note.
    payment_terms_days: Optional[int] = None
    is_active: bool = Field(default=True, index=True)
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class VendorCreate(SQLModel):
    name: str
    contact_name: Optional[str] = None
    contact_phone: Optional[str] = None
    contact_email: Optional[str] = None
    address: Optional[str] = None
    payment_terms_days: Optional[int] = None


class VendorUpdate(SQLModel):
    name: Optional[str] = None
    contact_name: Optional[str] = None
    contact_phone: Optional[str] = None
    contact_email: Optional[str] = None
    address: Optional[str] = None
    payment_terms_days: Optional[int] = None
    is_active: Optional[bool] = None


class Expense(SQLModel, table=True):
    """Individual expense record"""
    __tablename__ = "expenses"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)

    # Description and classification
    category: ExpenseCategory = Field(index=True)
    description: str  # What was the expense for
    vendor_name: Optional[str] = None  # Who was paid — free text, kept for backward compatibility and one-off payees
    vendor_id: Optional[str] = Field(default=None, index=True)  # Vendor.id, when this payee has a real master record
    
    # Amounts — Decimal/Numeric, matching the GL fields this posts against.
    amount: Decimal = Field(
        gt=Decimal("0"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES
    )  # Must be positive, in `currency`
    currency: str = Field(default="GHS")
    # Populated at posting time if currency != the school's base_currency —
    # the GL entry is always posted in base_currency, converted using the
    # exchange rate applicable on expense_date (see ExchangeRateService).
    base_currency_amount: Optional[Decimal] = Field(
        default=None, max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES
    )
    exchange_rate_applied: Optional[Decimal] = Field(default=None, max_digits=18, decimal_places=6)
    
    # GL mapping
    gl_account_id: Optional[str] = None  # Maps to GL account
    gl_account_code: Optional[str] = None  # Account code for reference
    
    # Dates
    expense_date: datetime = Field(index=True)  # When was expense incurred
    approved_date: Optional[datetime] = None
    approved_by: Optional[str] = None  # Admin who approved
    posted_date: Optional[datetime] = None  # When posted to GL ⭐ NEW
    posted_by: Optional[str] = None  # User who posted ⭐ NEW
    posted_ip: Optional[str] = None  # IP of user who posted ⭐ NEW
    
    # Approval workflow
    status: ExpenseStatus = Field(default=ExpenseStatus.DRAFT, index=True)
    submitted_by: Optional[str] = None  # User who submitted
    submitted_at: Optional[datetime] = None
    rejected_reason: Optional[str] = None
    rejected_by: Optional[str] = None
    
    # Period tracking
    fiscal_period_id: Optional[str] = None  # Link to fiscal period ⭐ NEW
    
    # Payment tracking
    # Distinct Postgres type name — models/fee.py defines its own, differently-valued
    # PaymentStatus enum that would otherwise collide on the default "paymentstatus" type name.
    payment_status: PaymentStatus = Field(
        default=PaymentStatus.OUTSTANDING,
        sa_column=Column(
            SQLEnum(PaymentStatus, name="expense_payment_status", values_callable=lambda x: [e.value for e in x]),
            index=True
        )
    )
    amount_paid: Decimal = Field(
        default=Decimal("0"), ge=Decimal("0"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES
    )
    payment_date: Optional[datetime] = None
    paid_by: Optional[str] = None
    
    # Journal entry link (if posted to GL)
    journal_entry_id: Optional[str] = None  # JE ID when posted
    gl_posting_reference: Optional[str] = None  # Reference like "JE-12345" ⭐ NEW

    # Supporting documentation (invoice/receipt image or PDF)
    receipt_url: Optional[str] = None
    
    # Audit
    notes: Optional[str] = None
    created_by: str  # User creating the expense
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ExpenseCreate(SQLModel):
    """Validation model for creating expenses"""
    category: ExpenseCategory
    description: str
    vendor_name: Optional[str] = None
    vendor_id: Optional[str] = None
    amount: Decimal
    currency: str = "GHS"
    gl_account_id: Optional[str] = None
    gl_account_code: Optional[str] = None
    expense_date: datetime
    notes: Optional[str] = None


class ExpenseUpdate(SQLModel):
    """Validation model for updating expenses"""
    category: Optional[ExpenseCategory] = None
    description: Optional[str] = None
    vendor_name: Optional[str] = None
    amount: Optional[Decimal] = None
    gl_account_id: Optional[str] = None
    gl_account_code: Optional[str] = None
    expense_date: Optional[datetime] = None
    notes: Optional[str] = None


class ExpenseSubmitRequest(SQLModel):
    """Request model for submitting expense for approval"""
    submission_notes: Optional[str] = None


class ExpenseApprovalRequest(SQLModel):
    """Request model for approving an expense"""
    approval_notes: Optional[str] = None


class ExpenseRejectionRequest(SQLModel):
    """Request model for rejecting an expense"""
    rejection_reason: str


class ExpensePaymentRequest(SQLModel):
    """Request model for recording expense payment"""
    amount_paid: Decimal
    payment_date: datetime
    payment_notes: Optional[str] = None


class ExpenseResponse(SQLModel):
    """Response model for expense"""
    id: str
    school_id: str
    category: ExpenseCategory
    description: str
    vendor_name: Optional[str]
    amount: Money
    currency: str
    base_currency_amount: Optional[Money] = None
    # Money's PlainSerializer only controls JSON output shape (Decimal ->
    # float), not rounding, so it's fine to reuse for a 6dp rate too — the
    # point is just avoiding the "12.500000" string Pydantic's default
    # Decimal JSON encoding would otherwise produce.
    exchange_rate_applied: Optional[Money] = None
    gl_account_id: Optional[str]
    gl_account_code: Optional[str]
    expense_date: datetime
    status: ExpenseStatus
    payment_status: PaymentStatus
    amount_paid: Money
    submitted_by: Optional[str]
    submitted_at: Optional[datetime]
    approved_by: Optional[str]
    approved_date: Optional[datetime]
    rejected_reason: Optional[str]
    journal_entry_id: Optional[str]
    receipt_url: Optional[str] = None
    notes: Optional[str]
    created_by: str
    created_at: datetime
    updated_at: datetime


# ==================== Summary & Analysis Models ====================

class ExpenseSummary(SQLModel):
    """Summary of expenses for analysis"""
    total_expenses: int
    draft_count: int
    pending_count: int
    approved_count: int
    posted_count: int
    rejected_count: int
    total_amount: Money
    total_paid: Money
    outstanding_amount: Money
    by_category: dict  # {category: {count, total_amount, total_paid}}


class ExpenseByCategory(SQLModel):
    """Breakdown of expenses by category"""
    category: ExpenseCategory
    count: int
    total_amount: Money
    total_paid: Money
    outstanding_amount: Money
    percentage_of_total: float
