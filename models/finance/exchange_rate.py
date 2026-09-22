"""Exchange Rate models — manual FX rates for converting non-base-currency
transactions into the school's base currency (School.base_currency).

Scope: single base currency per school, rates entered by hand (no external FX
API integration). A rate is looked up by (from_currency, to_currency) as of
a given date — the latest rate with effective_date <= that date wins, so a
school can update rates periodically without touching historical postings.
"""
from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime
from decimal import Decimal
import uuid

from .chart_of_accounts import Money


class ExchangeRate(SQLModel, table=True):
    """A manually-entered FX rate: 1 unit of from_currency = rate units of to_currency"""
    __tablename__ = "exchange_rates"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)

    from_currency: str = Field(index=True)  # e.g. "USD"
    to_currency: str = Field(index=True)    # e.g. "GHS" — usually the school's base_currency
    # Rates need more precision than money amounts (e.g. 1 USD = 12.345678 GHS)
    rate: Decimal = Field(max_digits=18, decimal_places=6)

    effective_date: datetime = Field(index=True)  # rate applies from this date onward
    notes: Optional[str] = None

    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ExchangeRateCreate(SQLModel):
    """Validation model for recording a new exchange rate"""
    from_currency: str
    to_currency: str
    rate: Decimal
    effective_date: datetime
    notes: Optional[str] = None


class ExchangeRateResponse(SQLModel):
    """Response model for an exchange rate"""
    id: str
    school_id: str
    from_currency: str
    to_currency: str
    rate: Money
    effective_date: datetime
    notes: Optional[str]
    created_by: str
    created_at: datetime
