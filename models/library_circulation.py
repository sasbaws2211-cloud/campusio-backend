"""Physical circulation models for the E-Library module: copies, loans,
fines, and reservations. Extends the digital catalog in models/library.py
rather than duplicating title metadata — a LibraryBookCopy always points at
an existing LibraryItem."""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from enum import Enum
import uuid

from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey, Integer


class CopyCondition(str, Enum):
    NEW = "new"
    GOOD = "good"
    FAIR = "fair"
    POOR = "poor"
    DAMAGED = "damaged"


class CopyStatus(str, Enum):
    AVAILABLE = "available"
    CHECKED_OUT = "checked_out"
    RESERVED = "reserved"
    LOST = "lost"
    RETIRED = "retired"


class LoanStatus(str, Enum):
    ACTIVE = "active"
    RETURNED = "returned"
    LOST = "lost"


class FineStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    WAIVED = "waived"


class ReservationStatus(str, Enum):
    PENDING = "pending"
    FULFILLED = "fulfilled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class LibraryBookCopy(SQLModel, table=True):
    __tablename__ = "library_book_copies"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    item_id: str = Field(sa_column=Column(String, ForeignKey("library_items.id", ondelete="CASCADE"), index=True))
    barcode: str = Field(index=True)
    accession_number: Optional[str] = None
    condition: str = CopyCondition.GOOD.value
    status: str = CopyStatus.AVAILABLE.value
    location: Optional[str] = None
    # Weeding/deaccessioning — set only via POST /copies/{id}/retire, kept
    # separate from a plain status edit so retiring a copy always carries a
    # reason and can't happen to one still checked out.
    retired_reason: Optional[str] = None
    retired_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class LibraryBookCopyCreate(SQLModel):
    item_id: str
    barcode: str
    accession_number: Optional[str] = None
    condition: str = CopyCondition.GOOD.value
    location: Optional[str] = None


class LibraryBookCopyUpdate(SQLModel):
    barcode: Optional[str] = None
    accession_number: Optional[str] = None
    condition: Optional[str] = None
    status: Optional[str] = None
    location: Optional[str] = None


class LibraryLoan(SQLModel, table=True):
    __tablename__ = "library_loans"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    copy_id: str = Field(sa_column=Column(String, ForeignKey("library_book_copies.id", ondelete="CASCADE"), index=True))
    borrower_user_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("users.id", ondelete="SET NULL"), index=True),
    )
    issued_by: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("users.id", ondelete="SET NULL")),
    )
    issue_date: str
    due_date: str
    return_date: Optional[str] = None
    status: str = LoanStatus.ACTIVE.value
    renewal_count: int = Field(default=0, sa_column=Column(Integer, nullable=False, server_default="0"))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class LibraryLoanCreate(SQLModel):
    barcode: Optional[str] = None
    copy_id: Optional[str] = None
    borrower_user_id: str
    due_date: Optional[str] = None


class LibraryLoanReportLost(SQLModel):
    replacement_cost: float = Field(gt=0)


class LibraryFine(SQLModel, table=True):
    __tablename__ = "library_fines"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    loan_id: str = Field(sa_column=Column(String, ForeignKey("library_loans.id", ondelete="CASCADE"), index=True))
    amount: float
    reason: str
    fee_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("fees.id", ondelete="SET NULL"), index=True),
    )
    status: str = FineStatus.PENDING.value
    waived_by: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("users.id", ondelete="SET NULL")),
    )
    waived_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class LibraryFineWaive(SQLModel):
    reason: str


class LibraryFinePayment(SQLModel):
    payment_method: str = "cash"


class LibraryFineSettings(SQLModel, table=True):
    """Per-school configuration for overdue library fines and borrowing
    policy — lets each school set its own daily rate/accrual cap and loan
    limits rather than a flat rule baked in for every school."""
    __tablename__ = "library_fine_settings"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True, unique=True)
    fine_per_day: float = Field(default=1.0)
    max_fine_days: int = Field(default=30)  # accrual stops increasing past this many days overdue
    max_loans_student: int = Field(default=3)
    max_loans_staff: int = Field(default=5)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class LibraryFineSettingsUpdate(SQLModel):
    fine_per_day: Optional[float] = None
    max_fine_days: Optional[int] = None
    max_loans_student: Optional[int] = None
    max_loans_staff: Optional[int] = None


class PatronStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class LibraryPatronStatus(SQLModel, table=True):
    """A borrower's standing with the library — separate from their
    school-wide User record, since being suspended from borrowing books
    has nothing to do with account access elsewhere in the system."""
    __tablename__ = "library_patron_status"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    user_id: str = Field(index=True)
    status: str = PatronStatus.ACTIVE.value
    reason: Optional[str] = None
    updated_by: Optional[str] = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class LibraryPatronStatusUpdate(SQLModel):
    status: PatronStatus
    reason: Optional[str] = None


class CopyRetire(SQLModel):
    reason: str


class LibraryReservation(SQLModel, table=True):
    __tablename__ = "library_reservations"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    item_id: str = Field(sa_column=Column(String, ForeignKey("library_items.id", ondelete="CASCADE"), index=True))
    user_id: str = Field(sa_column=Column(String, ForeignKey("users.id", ondelete="CASCADE"), index=True))
    status: str = ReservationStatus.PENDING.value
    reserved_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: Optional[datetime] = None
    fulfilled_loan_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("library_loans.id", ondelete="SET NULL")),
    )


class LibraryReservationCreate(SQLModel):
    item_id: str
    user_id: Optional[str] = None
