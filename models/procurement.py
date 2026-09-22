"""Supplier and purchase-order models for school procurement."""
from datetime import datetime
from enum import Enum
from typing import Optional
import uuid

from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey


class SupplierStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class PurchaseOrderStatus(str, Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    PARTIALLY_RECEIVED = "partially_received"
    RECEIVED = "received"
    CANCELLED = "cancelled"


class RequisitionStatus(str, Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    CONVERTED = "converted"
    CANCELLED = "cancelled"


class StockReturnStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class StockTransferStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class SupplierInvoiceStatus(str, Enum):
    OPEN = "open"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    VOID = "void"


class Supplier(SQLModel, table=True):
    __tablename__ = "suppliers"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    name: str
    contact_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    tax_number: Optional[str] = None
    status: SupplierStatus = SupplierStatus.ACTIVE
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class SupplierCreate(SQLModel):
    name: str
    contact_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    tax_number: Optional[str] = None


class SupplierUpdate(SQLModel):
    name: Optional[str] = None
    contact_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    tax_number: Optional[str] = None
    status: Optional[SupplierStatus] = None


class PurchaseOrder(SQLModel, table=True):
    __tablename__ = "purchase_orders"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    supplier_id: str = Field(sa_column=Column(String, ForeignKey("suppliers.id"), index=True))
    order_number: str = Field(index=True)
    order_date: str
    expected_date: Optional[str] = None
    status: PurchaseOrderStatus = PurchaseOrderStatus.DRAFT
    notes: Optional[str] = None
    created_by: str
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class PurchaseOrderLine(SQLModel, table=True):
    __tablename__ = "purchase_order_lines"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    purchase_order_id: str = Field(sa_column=Column(String, ForeignKey("purchase_orders.id", ondelete="CASCADE"), index=True))
    stock_item_id: str = Field(sa_column=Column(String, ForeignKey("stock_items.id"), index=True))
    quantity_ordered: float
    quantity_received: float = 0
    unit_cost: float = 0


class PurchaseOrderLineCreate(SQLModel):
    stock_item_id: str
    quantity_ordered: float
    unit_cost: float = 0


class PurchaseOrderCreate(SQLModel):
    supplier_id: str
    order_date: str
    expected_date: Optional[str] = None
    notes: Optional[str] = None
    lines: list[PurchaseOrderLineCreate]


class SupplierInvoice(SQLModel, table=True):
    __tablename__ = "supplier_invoices"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    supplier_id: str = Field(sa_column=Column(String, ForeignKey("suppliers.id"), index=True))
    purchase_order_id: Optional[str] = Field(default=None, sa_column=Column(String, ForeignKey("purchase_orders.id"), index=True))
    invoice_number: str = Field(index=True)
    invoice_date: str
    due_date: Optional[str] = None
    total_amount: float
    amount_paid: float = 0
    currency: str = "GHS"
    status: SupplierInvoiceStatus = SupplierInvoiceStatus.OPEN
    notes: Optional[str] = None
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class SupplierInvoiceCreate(SQLModel):
    supplier_id: str
    purchase_order_id: Optional[str] = None
    invoice_number: str
    invoice_date: str
    due_date: Optional[str] = None
    total_amount: float
    currency: str = "GHS"
    notes: Optional[str] = None


class SupplierInvoicePayment(SQLModel):
    amount: float


class PurchaseRequisition(SQLModel, table=True):
    __tablename__ = "purchase_requisitions"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    requisition_number: str = Field(index=True)
    requested_by: str = Field(index=True)
    department: Optional[str] = None
    purpose: str
    status: RequisitionStatus = RequisitionStatus.DRAFT
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class PurchaseRequisitionLine(SQLModel, table=True):
    __tablename__ = "purchase_requisition_lines"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    requisition_id: str = Field(sa_column=Column(String, ForeignKey("purchase_requisitions.id", ondelete="CASCADE"), index=True))
    stock_item_id: str = Field(sa_column=Column(String, ForeignKey("stock_items.id"), index=True))
    quantity_requested: float
    notes: Optional[str] = None


class RequisitionLineCreate(SQLModel):
    stock_item_id: str
    quantity_requested: float
    notes: Optional[str] = None


class PurchaseRequisitionCreate(SQLModel):
    department: Optional[str] = None
    purpose: str
    lines: list[RequisitionLineCreate]


class GoodsReceivedNote(SQLModel, table=True):
    __tablename__ = "goods_received_notes"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    grn_number: str = Field(index=True)
    purchase_order_id: str = Field(sa_column=Column(String, ForeignKey("purchase_orders.id"), index=True))
    received_by: str
    received_at: datetime = Field(default_factory=datetime.utcnow)
    notes: Optional[str] = None


class GoodsReceivedLine(SQLModel, table=True):
    __tablename__ = "goods_received_lines"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    grn_id: str = Field(sa_column=Column(String, ForeignKey("goods_received_notes.id", ondelete="CASCADE"), index=True))
    purchase_order_line_id: str = Field(index=True)
    quantity_received: float
    condition: str = "good"
    notes: Optional[str] = None


class StockReturn(SQLModel, table=True):
    __tablename__ = "stock_returns"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    stock_item_id: str = Field(sa_column=Column(String, ForeignKey("stock_items.id"), index=True))
    quantity: float
    reason: str
    condition: str = "damaged"
    status: StockReturnStatus = StockReturnStatus.PENDING
    requested_by: str
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class StockReturnCreate(SQLModel):
    stock_item_id: str
    quantity: float
    reason: str
    condition: str = "damaged"


class StockTransfer(SQLModel, table=True):
    __tablename__ = "stock_transfers"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    stock_item_id: str = Field(sa_column=Column(String, ForeignKey("stock_items.id"), index=True))
    quantity: float
    source_location: str
    destination_location: str
    reason: Optional[str] = None
    status: StockTransferStatus = StockTransferStatus.PENDING
    requested_by: str
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class StockTransferCreate(SQLModel):
    stock_item_id: str
    quantity: float
    source_location: str
    destination_location: str
    reason: Optional[str] = None