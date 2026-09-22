"""Inventory / Asset Management Models"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey
from typing import Optional
from datetime import datetime
from enum import Enum
import uuid


class IssuanceApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class AssetCondition(str, Enum):
    NEW = "new"
    GOOD = "good"
    FAIR = "fair"
    POOR = "poor"
    DAMAGED = "damaged"
    DISPOSED = "disposed"


class AssetStatus(str, Enum):
    IN_USE = "in_use"
    IN_STORAGE = "in_storage"
    UNDER_REPAIR = "under_repair"
    DISPOSED = "disposed"


class AssetCategory(SQLModel, table=True):
    """A grouping shared by both durable assets and consumable stock items"""
    __tablename__ = "asset_categories"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    name: str
    description: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AssetCategoryCreate(SQLModel):
    name: str
    description: Optional[str] = None


class AssetCategoryUpdate(SQLModel):
    name: Optional[str] = None
    description: Optional[str] = None


class Asset(SQLModel, table=True):
    """A durable, individually-tracked asset (furniture, equipment, etc.)"""
    __tablename__ = "assets"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    name: str
    tag_number: str = Field(index=True)

    category_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("asset_categories.id", ondelete="SET NULL"), index=True)
    )
    location: Optional[str] = None
    condition: AssetCondition = AssetCondition.GOOD
    status: AssetStatus = AssetStatus.IN_USE
    assigned_to_staff_id: Optional[str] = Field(default=None, index=True)

    purchase_date: Optional[str] = None
    purchase_cost: Optional[float] = None

    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AssetCreate(SQLModel):
    name: str
    tag_number: str
    category_id: Optional[str] = None
    location: Optional[str] = None
    condition: AssetCondition = AssetCondition.GOOD
    status: AssetStatus = AssetStatus.IN_USE
    assigned_to_staff_id: Optional[str] = None
    purchase_date: Optional[str] = None
    purchase_cost: Optional[float] = None
    notes: Optional[str] = None


class AssetUpdate(SQLModel):
    name: Optional[str] = None
    tag_number: Optional[str] = None
    category_id: Optional[str] = None
    location: Optional[str] = None
    condition: Optional[AssetCondition] = None
    status: Optional[AssetStatus] = None
    assigned_to_staff_id: Optional[str] = None
    purchase_date: Optional[str] = None
    purchase_cost: Optional[float] = None
    notes: Optional[str] = None


class StockItem(SQLModel, table=True):
    """A consumable stock item tracked by quantity, not individually"""
    __tablename__ = "stock_items"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    name: str

    category_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("asset_categories.id", ondelete="SET NULL"), index=True)
    )
    unit: str
    quantity_on_hand: float = 0
    reorder_level: Optional[int] = None
    location: Optional[str] = None

    # GL posting (optional) — when unit_cost and both account codes are set,
    # issuing this item posts a journal entry: Dr. gl_expense_account_code /
    # Cr. gl_inventory_asset_account_code. Both sides are school-configurable
    # (not a hardcoded constant) since a plausible-looking fixed account code
    # can collide with an unrelated account the school already has — e.g.
    # "1300" is "Fixed Assets - Building" in this deployment's seeded COA.
    # See routers/inventory.py::_post_stock_issuance_to_gl.
    unit_cost: Optional[float] = None
    gl_expense_account_code: Optional[str] = None
    gl_inventory_asset_account_code: Optional[str] = None

    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class StockItemCreate(SQLModel):
    name: str
    category_id: Optional[str] = None
    unit: str
    quantity_on_hand: float = 0
    reorder_level: Optional[int] = None
    location: Optional[str] = None
    unit_cost: Optional[float] = None
    gl_expense_account_code: Optional[str] = None
    gl_inventory_asset_account_code: Optional[str] = None
    notes: Optional[str] = None


class StockItemUpdate(SQLModel):
    name: Optional[str] = None
    category_id: Optional[str] = None
    unit_cost: Optional[float] = None
    gl_expense_account_code: Optional[str] = None
    gl_inventory_asset_account_code: Optional[str] = None
    unit: Optional[str] = None
    reorder_level: Optional[int] = None
    location: Optional[str] = None
    notes: Optional[str] = None


class StockIssuance(SQLModel, table=True):
    """An append-only log of stock issued out from inventory"""
    __tablename__ = "stock_issuances"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    stock_item_id: str = Field(sa_column=Column(String, ForeignKey("stock_items.id", ondelete="CASCADE"), index=True))

    issued_to: str
    issued_to_staff_id: Optional[str] = Field(default=None, index=True)
    quantity: float
    issue_date: str
    issued_by_staff_id: Optional[str] = None
    purpose: Optional[str] = None

    # Maker-checker (School.require_maker_checker, off by default): when
    # enabled, an issuance is created PENDING and the stock deduction + GL
    # posting below are deferred until a *different* user approves it via
    # routers/inventory.py::approve_stock_issuance. When disabled (the
    # default), issuances are auto-approved at creation — identical to this
    # module's original always-immediate behavior.
    approval_status: IssuanceApprovalStatus = IssuanceApprovalStatus.APPROVED
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None

    # Set when the issuance was successfully posted to the GL (see
    # routers/inventory.py::_post_stock_issuance_to_gl) — null if the stock
    # item has no GL config, or if posting was attempted but failed.
    gl_journal_entry_id: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)


class StockIssuanceCreate(SQLModel):
    issued_to: str
    issued_to_staff_id: Optional[str] = None
    quantity: float
    issue_date: str
    purpose: Optional[str] = None


class RejectIssuanceRequest(SQLModel):
    rejection_reason: Optional[str] = None
