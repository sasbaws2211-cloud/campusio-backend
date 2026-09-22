"""Journal Entry models for double-entry bookkeeping

Implements the accounting equation:
  Debits = Credits at all times

Every transaction is recorded with at least one debit and one credit
to maintain the balance sheet equation:
  Assets = Liabilities + Equity
"""
from sqlmodel import SQLModel, Field
from typing import Optional, List
from datetime import datetime
from decimal import Decimal
from enum import Enum
import uuid

from .chart_of_accounts import MONEY_MAX_DIGITS, MONEY_DECIMAL_PLACES, Money


class PostingStatus(str, Enum):
    """Status of a journal entry through its lifecycle"""
    DRAFT = "draft"              # Entry being prepared, not yet posted to GL
    POSTED = "posted"            # Entry has been posted to GL accounts
    REVERSED = "reversed"        # Entry was reversed with contra-entry
    REJECTED = "rejected"        # Entry was rejected and not posted


class ReferenceType(str, Enum):
    """Type of transaction creating the journal entry"""
    PAYROLL_RUN = "payroll_run"           # From payroll processing
    FEE_PAYMENT = "fee_payment"           # From student fee collection
    EXPENSE = "expense"                   # From expense processing
    MANUAL = "manual"                     # Manual journal entry
    ADJUSTMENT = "adjustment"             # Accounting adjustment
    DEPRECIATION = "depreciation"         # Period depreciation posting
    PERIOD_CLOSING = "period_closing"     # Year-end or period close
    BANK_RECONCILIATION = "bank_reconciliation"  # Bank clearing
    PLATFORM_SUBSCRIPTION = "platform_subscription"  # Platform usage fee
    ASSET_DISPOSAL = "asset_disposal"     # Fixed-asset disposal write-off + gain/loss
    FEE_INVOICE = "fee_invoice"           # Revenue recognized at fee invoice time (Dr AR / Cr Revenue)
    REFUND = "refund"                     # Refund liability recognized, or a refund actually paid out
    WRITE_OFF = "write_off"               # Uncollectable fee balance formally written off
    SETTLEMENT_WITHDRAWAL = "settlement_withdrawal"  # Paystack clearing -> real bank/MoMo


class JournalEntry(SQLModel, table=True):
    """Journal Entry - Header record for double-entry transaction
    
    A journal entry represents a complete accounting transaction that
    maintains the accounting equation (Debits = Credits).
    
    All journal entries must have:
    - At least one debit line item
    - At least one credit line item
    - Equal total debits and total credits
    """
    __tablename__ = "journal_entries"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    
    # Transaction identification
    entry_date: datetime = Field(index=True)
    reference_type: ReferenceType = Field(index=True)
    reference_id: Optional[str] = None  # Link to source: payroll_run_id, fee_payment_id, etc.
    description: str  # Human-readable description of transaction
    
    # Amounts — Decimal/Numeric, not float, so debit=credit comparisons are
    # exact rather than subject to binary-float rounding.
    total_debit: Decimal = Field(ge=Decimal("0"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES)
    total_credit: Decimal = Field(ge=Decimal("0"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES)
    
    # Status tracking
    posting_status: PostingStatus = Field(default=PostingStatus.DRAFT, index=True)
    posted_date: Optional[datetime] = None  # When posted to GL (NULL until posted)
    posted_by: Optional[str] = None  # User who posted the entry
    posted_ip: Optional[str] = None  # IP address of user who posted ⭐ NEW
    
    # Period and cutoff (for period management and cutoff procedures)
    fiscal_period_id: Optional[str] = None  # Link to fiscal period ⭐ NEW
    cutoff_period: Optional[int] = None  # Which period does this belong to ⭐ NEW
    is_adjusting_entry: bool = Field(default=False)  # Is this a period-end adjustment? ⭐ NEW
    # If set on a POSTED adjusting entry (e.g. an accrual), it's a candidate
    # for RecurringEntryService.reverse_due_accruals() once this date arrives —
    # nothing reverses it automatically before then.
    auto_reverse_date: Optional[datetime] = None
    
    # Rejection tracking (if applicable)
    rejection_reason: Optional[str] = None
    rejected_date: Optional[datetime] = None
    rejected_by: Optional[str] = None
    
    # Reversal tracking (if applicable)
    reversal_entry_id: Optional[str] = None  # Link to contra-entry if reversed
    reversed_date: Optional[datetime] = None
    reversed_by: Optional[str] = None
    reversal_reason: Optional[str] = None  # Why was it reversed? ⭐ NEW
    
    # Audit
    created_by: str  # User creating the entry
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class JournalLineItem(SQLModel, table=True):
    """Journal Line Item - Detail record of GL account posting
    
    Each line item records either a debit or credit to a specific GL account.
    A journal entry must have at least one debit and one credit line item.
    """
    __tablename__ = "journal_line_items"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    journal_entry_id: str = Field(index=True)  # FK to JournalEntry
    school_id: str = Field(index=True)
    
    # GL Account reference
    gl_account_id: str = Field(index=True)  # FK to GLAccount
    
    # Amount (either debit or credit, not both)
    debit_amount: Decimal = Field(
        default=Decimal("0"), ge=Decimal("0"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES
    )
    credit_amount: Decimal = Field(
        default=Decimal("0"), ge=Decimal("0"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES
    )
    
    # Description (can override entry description for detail)
    description: Optional[str] = None
    
    # Ordering for display
    line_number: int = Field(default=0)
    
    # Audit
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ==================== Request/Response Models ====================

class JournalLineItemCreate(SQLModel):
    """Validation model for creating journal line items"""
    gl_account_id: str
    debit_amount: Decimal = Decimal("0")
    credit_amount: Decimal = Decimal("0")
    description: Optional[str] = None
    line_number: int = 0


class JournalLineItemResponse(SQLModel):
    """Response model for line items"""
    id: str
    journal_entry_id: str
    gl_account_id: str
    debit_amount: Money
    credit_amount: Money
    description: Optional[str]
    line_number: int
    created_at: datetime


class JournalEntryCreate(SQLModel):
    """Validation model for creating journal entries
    
    Must include at least one debit line and one credit line.
    Debits and credits must balance exactly.
    """
    entry_date: datetime
    reference_type: ReferenceType
    reference_id: Optional[str] = None
    description: str
    line_items: List[JournalLineItemCreate]
    notes: Optional[str] = None
    # Adjusting entries (period-closing, accruals) may still post into a
    # LOCKED fiscal period if that period allows adjustments — see
    # FiscalPeriodService.can_post_to_period(). Defaults False for normal
    # entries, which a LOCKED period always rejects.
    is_adjusting_entry: bool = False
    # Set for accruals that should auto-reverse on a known future date (see
    # JournalEntry.auto_reverse_date).
    auto_reverse_date: Optional[datetime] = None


class JournalEntryUpdate(SQLModel):
    """Validation model for updating journal entries
    
    Only DRAFT entries can be updated.
    Posted entries must be reversed to correct them.
    """
    description: Optional[str] = None
    notes: Optional[str] = None
    line_items: Optional[List[JournalLineItemCreate]] = None


class JournalEntryResponse(SQLModel):
    """Response model for journal entries with line items"""
    id: str
    school_id: str
    entry_date: datetime
    reference_type: ReferenceType
    reference_id: Optional[str]
    description: str
    total_debit: Money
    total_credit: Money
    posting_status: PostingStatus
    posted_date: Optional[datetime]
    posted_by: Optional[str]
    reversal_entry_id: Optional[str]
    created_by: str
    notes: Optional[str]
    created_at: datetime
    updated_at: datetime
    line_items: Optional[List[JournalLineItemResponse]] = None


class JournalEntryPostRequest(SQLModel):
    """Request model for posting a journal entry to GL"""
    approval_notes: Optional[str] = None


class JournalEntryReverseRequest(SQLModel):
    """Request model for reversing a posted journal entry"""
    reversal_reason: str
    reversal_notes: Optional[str] = None


# ==================== Summary & Analysis Models ====================

class JournalEntrySummary(SQLModel):
    """Summary of journal entries for a period"""
    total_entries: int
    posted_entries: int
    draft_entries: int
    reversed_entries: int
    rejected_entries: int
    total_postings: int
    total_amount: Money


class TrialBalance(SQLModel):
    """Trial balance report for verification
    
    Used to verify that total debits = total credits
    """
    account_code: str
    account_name: str
    debit_balance: Money
    credit_balance: Money
    total_balance: Money
