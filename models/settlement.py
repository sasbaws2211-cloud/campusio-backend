"""Settlement and withdrawal models for school wallet management"""
from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime
from enum import Enum
import uuid


class WithdrawalStatus(str, Enum):
    """Status of a withdrawal"""
    PENDING = "pending"  # Initiated, waiting for Paystack
    PROCESSING = "processing"  # In progress
    COMPLETED = "completed"  # Successfully transferred
    FAILED = "failed"  # Transfer failed
    CANCELLED = "cancelled"  # Cancelled by user


class Withdrawal(SQLModel, table=True):
    """Track MoMo withdrawals from school wallet"""
    __tablename__ = "withdrawals"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    
    # Withdrawal details
    amount: float  # Amount in GHS
    momo_number: str  # Mobile money number
    recipient_name: str  # Name associated with MoMo account
    
    # Paystack details
    transfer_code: str = Field(index=True)  # Our tracking code
    paystack_transfer_id: Optional[str] = None  # Paystack's transfer ID
    recipient_code: Optional[str] = None  # Paystack recipient code
    
    # Status tracking
    status: WithdrawalStatus = WithdrawalStatus.PENDING
    failure_reason: Optional[str] = None

    # Paystack's transfer fee for this withdrawal, when known from the
    # gateway's own response — None means unknown, not zero (a completed
    # withdrawal is still posted to the GL without a fee line rather than
    # fabricating a number this codebase doesn't actually have).
    transfer_fee: Optional[float] = None
    # The GL entry posted when this withdrawal completes (Dr Business
    # Checking / Cr Paystack Clearing) — see
    # services/fee_gl_service.py::post_settlement_withdrawal. Previously a
    # completed withdrawal never touched the GL at all.
    journal_entry_id: Optional[str] = None
    
    # Timeline
    initiated_at: datetime = Field(default_factory=datetime.utcnow)
    initiated_by: str  # User ID who initiated
    completed_at: Optional[datetime] = None
    
    # Audit
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    
    class Config:
        str_strip_whitespace = True


class WithdrawalRead(SQLModel):
    """Read schema for Withdrawal"""
    id: str
    amount: float
    momo_number: str
    transfer_code: str
    status: WithdrawalStatus
    initiated_at: datetime
    completed_at: Optional[datetime] = None
