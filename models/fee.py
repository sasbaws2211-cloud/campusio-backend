"""Fee and Payment models"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey
from pydantic import model_validator
from typing import Optional
from datetime import datetime
from enum import Enum
import uuid


class FeeType(str, Enum):
    TUITION = "tuition"
    EXAMINATION = "examination"
    SPORTS = "sports"
    ICT = "ict"
    LIBRARY = "library"
    MAINTENANCE = "maintenance"
    PTA = "pta"
    OTHER = "other"


class PaymentStatus(str, Enum):
    PENDING = "pending"
    PARTIAL = "partial"
    PAID = "paid"
    OVERDUE = "overdue"
    # A formally written-off, uncollectable balance — previously there was
    # no terminal state for this at all; an unpaid balance for a withdrawn,
    # untraceable family just sat at PENDING/PARTIAL forever.
    WRITTEN_OFF = "written_off"


class PaymentMethod(str, Enum):
    CASH = "cash"
    BANK_TRANSFER = "bank_transfer"
    MOBILE_MONEY = "mobile_money"
    CHEQUE = "cheque"
    ONLINE_PAYMENT_PAYSTACK = "online_payment_paystack"


class FeeStructure(SQLModel, table=True):
    __tablename__ = "fee_structures"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    academic_term_id: Optional[str] = Field(default=None, sa_column=Column(String, ForeignKey("academic_terms.id", ondelete="SET NULL"), index=True))
    class_level: str
    # None = school-wide, applies regardless of campus (existing behavior).
    # Set = only assignable to classes in that campus — see assign_fee_to_class
    # in routers/fees.py, which enforces the match.
    campus_id: Optional[str] = Field(default=None, index=True)
    fee_type: FeeType
    amount: float
    description: Optional[str] = None
    is_mandatory: bool = True
    due_date: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class FeeStructureCreate(SQLModel):
    academic_term_id: str
    class_level: str
    campus_id: Optional[str] = None
    fee_type: FeeType
    amount: float = Field(gt=0)
    description: Optional[str] = None
    is_mandatory: bool = True
    due_date: str


class DiscountType(str, Enum):
    SIBLING = "sibling"
    STAFF_WARD = "staff_ward"
    SCHOLARSHIP = "scholarship"
    HARDSHIP = "hardship"
    PROMOTIONAL = "promotional"
    OTHER = "other"


class Fee(SQLModel, table=True):
    __tablename__ = "fees"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)
    academic_term_id: Optional[str] = Field(default=None, sa_column=Column(String, ForeignKey("academic_terms.id", ondelete="SET NULL"), index=True))
    fee_structure_id: str = Field(index=True)
    amount_due: float
    amount_paid: float = 0
    discount: float = 0
    discount_reason: Optional[str] = None
    discount_type: Optional[DiscountType] = None
    discount_approved_by: Optional[str] = None
    status: PaymentStatus = PaymentStatus.PENDING
    # Set once by services/fee_late_fee_service.py when a penalty is charged
    # for going overdue; folded directly into amount_due at that moment (see
    # that service). Kept here purely as an idempotency marker/audit value —
    # every other call site's `amount_due - amount_paid - discount` balance
    # math needs no changes since the penalty already lives in amount_due.
    late_fee_amount: float = 0
    # The journal entry that recognized this fee as revenue at invoice
    # time (Dr Accounts Receivable / Cr Revenue) — see services/fee_gl_service.py.
    # None for a fee created before this existed, or if GL posting failed
    # at creation time (logged separately then).
    invoice_journal_entry_id: Optional[str] = None
    written_off_at: Optional[datetime] = None
    written_off_by: Optional[str] = None
    write_off_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class FeeCreate(SQLModel):
    student_id: str
    academic_term_id: str
    fee_structure_id: str
    amount_due: float = Field(gt=0)
    discount: float = Field(default=0, ge=0)
    discount_type: Optional[DiscountType] = None
    discount_reason: Optional[str] = None

    @model_validator(mode="after")
    def check_discount(self):
        if self.discount > self.amount_due:
            raise ValueError("discount cannot exceed amount_due")
        if self.discount > 0 and self.discount_type is None:
            raise ValueError("discount_type is required when a discount is applied")
        return self


class FeeDiscountUpdate(SQLModel):
    discount: float = Field(ge=0)
    discount_type: DiscountType
    discount_reason: Optional[str] = None


class FeePayment(SQLModel, table=True):
    __tablename__ = "fee_payments"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    fee_id: str = Field(index=True)
    student_id: str = Field(index=True)
    amount: float
    payment_method: PaymentMethod
    reference_number: Optional[str] = None
    receipt_number: str
    payment_date: str
    remarks: Optional[str] = None
    received_by: str
    # The GL journal entry _create_fee_journal_entry posted for this payment
    # (routers/fees.py) — recorded so void_payment can reverse it via
    # JournalEntryService.reverse_entry instead of leaving the ledger
    # permanently overstating cash/revenue after a void. None if GL posting
    # failed at record-payment time (already logged separately then).
    journal_entry_id: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    voided: bool = Field(default=False, index=True)
    voided_at: Optional[datetime] = None
    voided_by: Optional[str] = None
    void_reason: Optional[str] = None


class ReceiptSequence(SQLModel, table=True):
    """Per-school, per-year gapless receipt-number counter — see
    services/receipt_sequence_service.py::get_next_receipt_number. Previously
    every FeePayment.receipt_number was a random UUID fragment
    ("RCP-20260909-A1B2C3D4"), which looks sequential but isn't: it can't be
    used to prove no receipt was skipped or back-dated, the way a real
    numbered receipt register can. One row per (school_id, year); a new
    year starts back at 1."""
    __tablename__ = "receipt_sequences"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    year: int = Field(index=True)
    last_number: int = 0


class FeePaymentCreate(SQLModel):
    fee_id: str
    amount: float = Field(gt=0)
    payment_method: PaymentMethod
    reference_number: Optional[str] = None
    payment_date: str
    remarks: Optional[str] = None


class VoidPaymentRequest(SQLModel):
    reason: str


class FeeWriteOffRequest(SQLModel):
    reason: str


class InstallmentStatus(str, Enum):
    PENDING = "pending"
    PARTIAL = "partial"
    PAID = "paid"
    OVERDUE = "overdue"


class FeeInstallment(SQLModel, table=True):
    """A scheduled slice of a Fee's amount_due. Fee.amount_due/amount_paid
    remain the source of truth for the overall balance — installments are a
    due-date breakdown of that same total, kept in sync as payments come in
    (see _allocate_to_installments in routers/fees.py)."""
    __tablename__ = "fee_installments"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    fee_id: str = Field(index=True)
    student_id: str = Field(index=True)
    installment_number: int
    amount_due: float
    amount_paid: float = 0
    due_date: str
    status: InstallmentStatus = InstallmentStatus.PENDING
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class FeeInstallmentScheduleItem(SQLModel):
    amount_due: float = Field(gt=0)
    due_date: str


class FeeInstallmentPlanCreate(SQLModel):
    installments: list[FeeInstallmentScheduleItem]

    @model_validator(mode="after")
    def check_installments_present(self):
        if not self.installments:
            raise ValueError("installments must not be empty")
        return self
