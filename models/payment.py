"""Online payment models for Paystack integration"""
from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime
from enum import Enum
import uuid


class TransactionStatus(str, Enum):
    """Payment transaction status"""
    PENDING = "pending"  # Waiting for parent action
    PROCESSING = "processing"  # Payment being processed
    SUCCESS = "success"  # Payment successful
    FAILED = "failed"  # Payment failed
    CANCELLED = "cancelled"  # Parent cancelled
    WEBHOOK_RECEIVED = "webhook_received"  # Webhook received, verifying


class PaymentGateway(str, Enum):
    """Supported payment gateways"""
    PAYSTACK = "paystack"
    FLUTTERWAVE = "flutterwave"
    JUMIAPAY = "jumiapay"


class TransactionType(str, Enum):
    """Type of online transaction"""
    FEE = "fee"  # Student fee payment
    SUBSCRIPTION = "subscription"  # Platform subscription payment
    CANTEEN_TOPUP = "canteen_topup"  # Parent funding a student's canteen wallet
    EXTRA_CLASS_FEE = "extra_class_fee"  # Parent paying a teacher-led extra class billing cycle
    ADMISSION_FEE = "admission_fee"  # Public applicant paying an admissions application fee
    ADMISSION_DEPOSIT = "admission_deposit"  # Public applicant paying a seat-deposit after accepting an offer


class OnlineTransaction(SQLModel, table=True):
    """Track all online payment attempts"""
    __tablename__ = "online_transactions"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    
    # Reference to fee being paid
    fee_id: str = Field(index=True)
    student_id: str = Field(index=True)
    parent_id: str = Field(index=True)
    # Set only for transaction_type == EXTRA_CLASS_FEE — which billing
    # cycle this payment settles.
    billing_cycle_id: Optional[str] = Field(default=None, index=True)
    
    # Payment details
    amount: float  # Amount requested (GHS)
    amount_paid: float = 0  # Amount actually paid
    currency: str = "GHS"
    
    # Transaction classification
    transaction_type: TransactionType = TransactionType.FEE  # Type of transaction (fee or subscription)
    
    # Gateway info
    gateway: str = PaymentGateway.PAYSTACK  # "paystack", "flutterwave", etc.
    reference: str = Field(unique=True, index=True)  # Paystack reference — the payment idempotency key (uq_online_txn_reference)
    access_code: Optional[str] = None  # Paystack access code
    payment_url: Optional[str] = None  # URL for parent to click
    
    # Transaction status
    status: TransactionStatus = TransactionStatus.PENDING
    payment_status: Optional[str] = None  # From gateway response
    
    # Tracking
    initiated_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    failed_reason: Optional[str] = None
    gateway_response: Optional[str] = None  # JSON from gateway
    
    # Reconciliation
    verified_at: Optional[datetime] = None
    journal_entry_id: Optional[str] = None  # Link to GL entry created

    # Refunds — set when a SUCCESS transaction paid more than was owed (two
    # checkout links, a student with no more outstanding fees, etc). Money
    # genuinely arrived so the transaction stays SUCCESS; this just tracks
    # that the excess is owed back. No live Paystack refund API call is
    # made — this is a manual queue an admin works off, same as bank
    # reconciliation elsewhere in this system.
    refund_status: Optional[str] = None  # None, "pending", "completed"
    refund_amount: Optional[float] = None
    refunded_at: Optional[datetime] = None
    refunded_by: Optional[str] = None  # User.id who marked it refunded
    refund_notes: Optional[str] = None
    # GL entry recognizing the overpayment as a liability the moment it's
    # flagged (Dr Bank/Clearing / Cr Refunds Payable) — see
    # services/fee_gl_service.py::post_refund_liability. Previously this
    # cash had zero GL representation until/unless someone remembered to
    # refund it, silently understating GL cash for that whole window.
    refund_liability_journal_entry_id: Optional[str] = None
    # GL entry for the refund actually being paid out (Dr Refunds Payable /
    # Cr Bank/Clearing) — set once mark_fee_refund_completed runs.
    refund_payout_journal_entry_id: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class OnlineTransactionRead(SQLModel):
    """Read schema for OnlineTransaction"""
    id: str
    fee_id: str
    student_id: str
    amount: float
    amount_paid: float
    transaction_type: TransactionType
    status: TransactionStatus
    initiated_at: datetime
    completed_at: Optional[datetime]
    payment_url: Optional[str]
    reference: str


class PaymentVerification(SQLModel, table=True):
    """Track webhook verification for auditing"""
    __tablename__ = "payment_verifications"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    transaction_id: str = Field(index=True)
    
    # What we verified
    gateway: str
    reference: str
    expected_amount: float
    actual_amount: float
    
    # Verification result
    verified: bool
    match_status: str  # AMOUNT_MATCH, AMOUNT_MISMATCH, REFERENCE_MISMATCH, etc.
    verified_at: datetime = Field(default_factory=datetime.utcnow)
    
    webhook_payload: Optional[str] = None  # Save for debugging
