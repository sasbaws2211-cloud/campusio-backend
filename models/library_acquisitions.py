"""Library acquisitions — ordering new titles, linked to the school's real
procurement workflow (models/procurement.py's Supplier/PurchaseOrder)
rather than a library-only wishlist disconnected from actual purchasing."""
from datetime import datetime
from enum import Enum
from typing import Optional
import uuid

from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey


class AcquisitionStatus(str, Enum):
    REQUESTED = "requested"
    ORDERED = "ordered"
    RECEIVED = "received"
    CANCELLED = "cancelled"


class LibraryAcquisitionRequest(SQLModel, table=True):
    __tablename__ = "library_acquisition_requests"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    title: str
    author: Optional[str] = None
    isbn: Optional[str] = None
    category_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("library_categories.id", ondelete="SET NULL"), index=True),
    )
    quantity_requested: int = Field(default=1, gt=0)
    quantity_received: int = Field(default=0)
    status: str = AcquisitionStatus.REQUESTED.value
    # Linked once a school actually raises a purchase order for this
    # request — see POST /library/acquisitions/{id}/link-purchase-order.
    purchase_order_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("purchase_orders.id", ondelete="SET NULL"), index=True),
    )
    # Set once received and the catalog entry/copies have been created —
    # see POST /library/acquisitions/{id}/receive.
    library_item_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("library_items.id", ondelete="SET NULL"), index=True),
    )
    requested_by: str
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class LibraryAcquisitionRequestCreate(SQLModel):
    title: str
    author: Optional[str] = None
    isbn: Optional[str] = None
    category_id: Optional[str] = None
    quantity_requested: int = Field(default=1, gt=0)
    notes: Optional[str] = None


class LibraryAcquisitionLinkPO(SQLModel):
    purchase_order_id: str


class LibraryAcquisitionReceive(SQLModel):
    quantity_received: int = Field(gt=0)
    location: Optional[str] = None
