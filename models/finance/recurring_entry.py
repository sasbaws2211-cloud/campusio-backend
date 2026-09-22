"""Recurring Journal Entry Template models

A template describes a journal entry that repeats on a schedule (e.g.
monthly rent, quarterly insurance). Running RecurringEntryService's
generate-due step creates a real DRAFT journal entry from every template
whose next_run_date has arrived — nothing runs on its own (see
RecurringEntryService docstring for why this is admin-triggered, not a
background scheduler).
"""
from sqlmodel import SQLModel, Field
from typing import Optional, List
from datetime import datetime
from decimal import Decimal
from enum import Enum
import uuid

from .chart_of_accounts import MONEY_MAX_DIGITS, MONEY_DECIMAL_PLACES, Money


class RecurrenceFrequency(str, Enum):
    """How often a recurring entry template repeats"""
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    SEMI_ANNUAL = "semi_annual"
    ANNUAL = "annual"


class RecurringJournalEntryTemplate(SQLModel, table=True):
    """A journal entry template that repeats on a schedule"""
    __tablename__ = "recurring_journal_entry_templates"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)

    description: str
    frequency: RecurrenceFrequency = Field(index=True)
    next_run_date: datetime = Field(index=True)
    end_date: Optional[datetime] = None  # stop generating after this date, if set

    is_active: bool = Field(default=True, index=True)

    # If set, entries generated from this template are marked as adjusting
    # entries and auto-reversed `auto_reverse_after_days` days after posting
    # (e.g. a monthly accrual reversed at the start of the next month).
    is_accrual: bool = Field(default=False)
    auto_reverse_after_days: Optional[int] = None

    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class RecurringEntryLineTemplate(SQLModel, table=True):
    """One GL line within a recurring entry template"""
    __tablename__ = "recurring_entry_line_templates"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    template_id: str = Field(index=True)
    school_id: str = Field(index=True)

    gl_account_id: str = Field(index=True)
    debit_amount: Decimal = Field(
        default=Decimal("0"), ge=Decimal("0"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES
    )
    credit_amount: Decimal = Field(
        default=Decimal("0"), ge=Decimal("0"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES
    )
    description: Optional[str] = None
    line_number: int = Field(default=0)


# ==================== Request/Response Models ====================

class RecurringEntryLineTemplateCreate(SQLModel):
    """Validation model for a recurring entry template line"""
    gl_account_id: str
    debit_amount: Decimal = Decimal("0")
    credit_amount: Decimal = Decimal("0")
    description: Optional[str] = None
    line_number: int = 0


class RecurringJournalEntryTemplateCreate(SQLModel):
    """Validation model for creating a recurring entry template"""
    description: str
    frequency: RecurrenceFrequency
    next_run_date: datetime
    end_date: Optional[datetime] = None
    is_accrual: bool = False
    auto_reverse_after_days: Optional[int] = None
    line_items: List[RecurringEntryLineTemplateCreate]


class RecurringEntryLineTemplateResponse(SQLModel):
    """Response model for a recurring entry template line"""
    id: str
    gl_account_id: str
    debit_amount: Money
    credit_amount: Money
    description: Optional[str]
    line_number: int


class RecurringJournalEntryTemplateResponse(SQLModel):
    """Response model for a recurring entry template"""
    id: str
    school_id: str
    description: str
    frequency: RecurrenceFrequency
    next_run_date: datetime
    end_date: Optional[datetime]
    is_active: bool
    is_accrual: bool
    auto_reverse_after_days: Optional[int]
    created_by: str
    created_at: datetime
    updated_at: datetime
    line_items: Optional[List[RecurringEntryLineTemplateResponse]] = None
