"""Chart of Accounts models for general ledger management"""
from sqlmodel import SQLModel, Field
from typing import Optional, Annotated
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pydantic import PlainSerializer
import uuid

# Precision for all money fields: 14 integer digits, 2 decimal places.
# Plenty of headroom for school-level GL amounts while keeping every balance
# an exact Decimal instead of a binary float that can't represent 0.1 exactly.
MONEY_MAX_DIGITS = 14
MONEY_DECIMAL_PLACES = 2

# Use in *response* models only (not table models or Create/Update inputs).
# Pydantic v2's default JSON encoding renders Decimal as a *string* (to
# preserve precision) — fine for storage, but every finance API response
# would otherwise start returning "123.45" instead of 123.45, breaking
# frontend code that does arithmetic or .toFixed() on these values. This
# keeps Decimal as the Python/validation type but serializes to a JSON
# number, matching what the API returned before this field was Decimal.
Money = Annotated[Decimal, PlainSerializer(lambda v: float(v), return_type=float, when_used="json")]


class AccountType(str, Enum):
    """Primary account classification for balance sheet and income statement"""
    ASSET = "asset"              # Bank, accounts receivable, inventory
    LIABILITY = "liability"       # Accounts payable, salaries payable
    EQUITY = "equity"             # Accumulated surplus/deficit
    REVENUE = "revenue"           # Income streams (tuition, donations)
    EXPENSE = "expense"           # Costs (salaries, utilities, supplies)


class AccountCategory(str, Enum):
    """Subcategories for better reporting and organization"""
    # Assets
    BANK_ACCOUNTS = "bank_accounts"
    ACCOUNTS_RECEIVABLE = "accounts_receivable"
    PREPAID_EXPENSES = "prepaid_expenses"
    FIXED_ASSETS = "fixed_assets"
    
    # Liabilities
    ACCOUNTS_PAYABLE = "accounts_payable"
    SALARIES_PAYABLE = "salaries_payable"
    SHORT_TERM_DEBT = "short_term_debt"
    LONG_TERM_DEBT = "long_term_debt"
    
    # Equity
    ACCUMULATED_SURPLUS = "accumulated_surplus"
    RETAINED_EARNINGS = "retained_earnings"
    
    # Revenue
    STUDENT_FEES = "student_fees"
    DONATIONS = "donations"
    GRANTS = "grants"
    OTHER_INCOME = "other_income"
    
    # Expense
    SALARIES_WAGES = "salaries_wages"
    UTILITIES = "utilities"
    SUPPLIES = "supplies"
    REPAIRS_MAINTENANCE = "repairs_maintenance"
    TRANSPORT_COSTS = "transport_costs"
    CONTRACTED_SERVICES = "contracted_services"
    DEPRECIATION = "depreciation"
    OTHER_EXPENSES = "other_expenses"


class GLAccount(SQLModel, table=True):
    """General Ledger Account - fundamental accounting record
    
    Each account represents a specific financial item that can be debited or credited.
    Accounts must follow accounting equation: Assets = Liabilities + Equity
    And: Revenue - Expenses = Net Income
    
    The chart of accounts is hierarchical to support sub-ledgers.
    """
    __tablename__ = "gl_accounts"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    
    # Identification
    account_code: str = Field(index=True)  # e.g., "1010", "5100", "4000" - Unique per school
    account_name: str                      # e.g., "Business Checking Account"
    
    # Classification
    account_type: AccountType = Field(index=True)
    account_category: AccountCategory
    
    # Details
    description: Optional[str] = None
    normal_balance: str = Field(default="debit")  # "debit" or "credit" - which side increases the balance
    
    # Hierarchy (for sub-ledgers and reporting structure) — this is the ONE
    # natural chart-of-accounts tree (e.g. "1100 AR" nests under "1000
    # Assets"). models/finance/account_hierarchy.py is a deliberately
    # separate feature for MULTIPLE overlapping rollup dimensions on top of
    # this tree (organizational/cost-center, functional, program/fund) —
    # not a duplicate of this field, see that module's docstring.
    parent_account_id: Optional[str] = None  # For creating hierarchical account structures

    # System role: marks an account as filling a well-known function (e.g.
    # "default_cash_account", "retained_earnings") that other services look up
    # by role instead of a hardcoded account_code. Lets a school rename/replace
    # which account fills that role without breaking expense posting or period
    # close. None means the account has no special system role.
    system_role: Optional[str] = Field(default=None, index=True)

    # Status
    is_active: bool = Field(default=True, index=True)
    
    # ⭐ BALANCE TRACKING (CRITICAL FOR PERFORMANCE & ACCURACY)
    # Decimal/Numeric, not float — a binary float can't represent amounts
    # like 0.10 exactly, which compounds into real drift across thousands of
    # postings on a denormalized running balance like this one.
    current_balance: Decimal = Field(
        default=Decimal("0"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES
    )  # Denormalized balance for performance
    opening_balance: Decimal = Field(
        default=Decimal("0"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES
    )  # Period opening balance (for comparisons)
    bank_reconciled_balance: Optional[Decimal] = Field(
        default=None, max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES
    )  # Last reconciled balance
    last_balance_update: datetime = Field(default_factory=datetime.utcnow)  # When balance was last updated
    bank_reconciliation_date: Optional[datetime] = None  # When last reconciled to bank
    reconciliation_notes: Optional[str] = None  # Notes on bank reconciliation
    
    # Tracking
    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    
    class Config:
        """Ensure unique account codes per school"""
        # Note: Index created via migration for (school_id, account_code) composite key
        pass


class GLAccountCreate(SQLModel):
    """Validation model for creating GL accounts"""
    account_code: str
    account_name: str
    account_type: AccountType
    account_category: AccountCategory
    description: Optional[str] = None
    # None means "derive from account_type" (see CoaService.create_account) — only set this
    # explicitly for contra accounts (e.g. Accumulated Depreciation, an ASSET with a credit balance).
    normal_balance: Optional[str] = None
    parent_account_id: Optional[str] = None
    system_role: Optional[str] = None


class GLAccountUpdate(SQLModel):
    """Validation model for updating GL accounts"""
    account_name: Optional[str] = None
    account_category: Optional[AccountCategory] = None
    description: Optional[str] = None
    normal_balance: Optional[str] = None
    is_active: Optional[bool] = None
    system_role: Optional[str] = None


class GLAccountResponse(SQLModel):
    """Response model for GL account queries"""
    id: str
    school_id: str
    account_code: str
    account_name: str
    account_type: AccountType
    account_category: AccountCategory
    description: Optional[str]
    normal_balance: str
    parent_account_id: Optional[str]
    system_role: Optional[str] = None
    is_active: bool
    # ⭐ BALANCE FIELDS (NEW)
    current_balance: Money
    opening_balance: Money
    bank_reconciled_balance: Optional[Money]
    last_balance_update: datetime
    created_by: Optional[str]
    created_at: datetime
    updated_at: datetime
