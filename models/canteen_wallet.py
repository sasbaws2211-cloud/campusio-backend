from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
import uuid

from sqlmodel import Field, SQLModel
from sqlalchemy import Column, String, ForeignKey


class CanteenWalletAccount(SQLModel, table=True):
    __tablename__ = "canteen_wallet_accounts"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)
    parent_id: Optional[str] = Field(default=None, index=True)
    balance: float = 0.0

    # Legacy postpaid-credit field — no longer written to by new purchases
    # (the wallet is real prepaid money now, see CanteenOrder). Kept so old
    # rows/history aren't lost; always 0 going forward.
    pending_parent_settlement: float = 0.0

    # Parent-set spending controls
    frozen: bool = False
    daily_limit: Optional[float] = None
    weekly_limit: Optional[float] = None
    blocked_categories: Optional[str] = None  # comma-separated CanteenItem.item_type values
    low_balance_threshold: float = 10.0

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class CanteenItem(SQLModel, table=True):
    __tablename__ = "canteen_items"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    name: str = Field(index=True)
    # Doubles as the "category" used for menu grouping and parent
    # blocked-category controls: breakfast/lunch/snacks/drinks/stationery.
    item_type: str = Field(default="lunch", index=True)
    price: float = 0.0
    info: Optional[str] = None
    code: str = Field(index=True, unique=True)
    is_active: bool = True
    # False = grab-and-go (packaged snacks, drinks, stationery) — an order
    # containing only these skips straight to READY, no kitchen queue. True
    # = made-to-order (hot meals) — order goes through the normal
    # pending/accepted/preparing flow. A mixed cart follows the strictest
    # item's requirement.
    needs_prep: bool = False
    # None = unlimited/untracked stock (default for schools that don't want
    # inventory management); a number enables the out-of-stock gate.
    stock_count: Optional[int] = None
    # Nutritional info — all optional since existing items were created
    # without them and most schools won't backfill every field.
    calories: Optional[int] = None
    protein_g: Optional[float] = None
    carbs_g: Optional[float] = None
    fat_g: Optional[float] = None
    allergens: Optional[str] = None  # comma-separated
    # Vendor management — links to the existing procurement Supplier table
    # rather than a new canteen-specific vendor model.
    supplier_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("suppliers.id", ondelete="SET NULL"), index=True)
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class CanteenWalletLedgerEntry(SQLModel, table=True):
    __tablename__ = "canteen_wallet_ledger_entries"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    wallet_id: str = Field(index=True)
    event_type: str = Field(index=True)
    amount: float = 0.0
    reference: Optional[str] = None
    description: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class CanteenOrderStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    PREPARING = "preparing"
    READY = "ready"
    COMPLETED = "completed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class CanteenOrder(SQLModel, table=True):
    __tablename__ = "canteen_orders"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)
    parent_id: Optional[str] = Field(default=None, index=True)
    order_code: str = Field(index=True, unique=True)
    status: CanteenOrderStatus = Field(default=CanteenOrderStatus.PENDING, index=True)
    subtotal: float = 0.0
    total: float = 0.0
    # Shown to the student as a QR/short code; canteen staff scans or types
    # it at collection to confirm the right pupil and mark the order done.
    pickup_code: str = Field(index=True)
    note: Optional[str] = None
    rejection_reason: Optional[str] = None
    accepted_at: Optional[datetime] = None
    preparing_at: Optional[datetime] = None
    ready_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None  # rejected or cancelled
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class CanteenOrderItem(SQLModel, table=True):
    __tablename__ = "canteen_order_items"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    order_id: str = Field(index=True)
    item_id: Optional[str] = Field(default=None, index=True)
    item_name: str
    unit_price: float
    quantity: int
    line_total: float
