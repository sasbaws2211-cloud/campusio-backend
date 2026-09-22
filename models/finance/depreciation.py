"""Depreciation Schedule models — straight-line depreciation of a fixed
asset, run monthly via DepreciationService.generate_due_depreciation()
(admin-triggered, same pattern as recurring entries — see
services/recurring_entry_service.py for why).
"""
from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime
from decimal import Decimal
from enum import Enum
import uuid

from .chart_of_accounts import MONEY_MAX_DIGITS, MONEY_DECIMAL_PLACES, Money


class DepreciationMethod(str, Enum):
    """Depreciation calculation method"""
    STRAIGHT_LINE = "straight_line"


class DepreciationSchedule(SQLModel, table=True):
    """A fixed asset's depreciation schedule"""
    __tablename__ = "depreciation_schedules"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)

    asset_description: str
    # Optional link to the fixed-asset register (models/inventory.py Asset) —
    # when set, disposing this schedule also flips that Asset's
    # status/condition to DISPOSED. Nullable since most schedules predate
    # this link and asset_description alone remains valid on its own.
    asset_id: Optional[str] = Field(default=None, index=True)
    asset_account_id: str = Field(index=True)  # the fixed-asset GL account (informational)
    accumulated_depreciation_account_id: str = Field(index=True)  # credited each run
    depreciation_expense_account_id: str = Field(index=True)  # debited each run

    method: DepreciationMethod = Field(default=DepreciationMethod.STRAIGHT_LINE)
    asset_cost: Decimal = Field(gt=Decimal("0"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES)
    salvage_value: Decimal = Field(
        default=Decimal("0"), ge=Decimal("0"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES
    )
    useful_life_months: int = Field(gt=0)
    # Computed at creation: (asset_cost - salvage_value) / useful_life_months
    monthly_depreciation_amount: Decimal = Field(max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES)

    start_date: datetime
    next_run_date: datetime = Field(index=True)
    periods_run: int = Field(default=0)  # how many monthly entries have posted so far
    is_active: bool = Field(default=True, index=True)

    # Set once by DepreciationService.dispose_asset — distinguishes a
    # disposal from a plain deactivate_schedule() stop (is_active=False with
    # these left null).
    disposed_at: Optional[datetime] = None
    disposal_journal_entry_id: Optional[str] = None

    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class DepreciationScheduleCreate(SQLModel):
    """Validation model for creating a depreciation schedule"""
    asset_description: str
    asset_id: Optional[str] = None
    asset_account_id: str
    accumulated_depreciation_account_id: str
    depreciation_expense_account_id: str
    method: DepreciationMethod = DepreciationMethod.STRAIGHT_LINE
    asset_cost: Decimal
    salvage_value: Decimal = Decimal("0")
    useful_life_months: int
    start_date: datetime


class DepreciationScheduleResponse(SQLModel):
    """Response model for a depreciation schedule"""
    id: str
    school_id: str
    asset_description: str
    asset_id: Optional[str]
    asset_account_id: str
    accumulated_depreciation_account_id: str
    depreciation_expense_account_id: str
    method: DepreciationMethod
    asset_cost: Money
    salvage_value: Money
    useful_life_months: int
    monthly_depreciation_amount: Money
    start_date: datetime
    next_run_date: datetime
    periods_run: int
    is_active: bool
    disposed_at: Optional[datetime]
    disposal_journal_entry_id: Optional[str]
    created_by: str
    created_at: datetime
    updated_at: datetime


class DisposeAssetRequest(SQLModel):
    """Request body for writing off a depreciation schedule's asset"""
    disposal_date: datetime
    proceeds: Decimal = Decimal("0")
    gain_loss_account_id: str
    cash_account_id: Optional[str] = None  # required if proceeds > 0


class DisposeAssetResponse(SQLModel):
    schedule_id: str
    journal_entry_id: str
    net_book_value: Money
    accumulated_depreciation: Money
    gain_or_loss: Money
