"""Platform subscription billing models"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey
from typing import Optional
from datetime import datetime
from enum import Enum
import uuid


class SubscriptionStatus(str, Enum):
    """Platform subscription status"""
    PENDING = "pending"          # Awaiting payment
    ACTIVE = "active"            # Paid and active
    SUSPENDED = "suspended"      # Overdue or manually suspended
    CANCELLED = "cancelled"      # Subscription ended


class BillingPlan(str, Enum):
    """How a school is billed for the platform.

    Stored as a plain String column (not a native Postgres enum) deliberately —
    this codebase has been bitten repeatedly by native-enum name/value mismatches.
    """
    TERMLY = "termly"    # GHS per student per academic term (default)
    MONTHLY = "monthly"  # GHS per student per calendar month (cash-flow flexibility)


class PlanTier(str, Enum):
    """Feature entitlement tier — distinct from BillingPlan, which only
    controls billing cadence. This is the value ladder: which optional
    modules a school's users can reach. See services/plan_gating.py for
    what each tier actually unlocks.

    Plain String column, same reasoning as BillingPlan above.

    STARTER's stored value is unchanged from before STANDARD existed —
    only its *display* label became "Basic" (see plan_gating.py's
    _TIER_DISPLAY_NAMES) when a 4th tier was inserted between it and
    GROWTH. Renaming the stored value would need a data migration for
    zero benefit, so the value/member name stays STARTER.
    """
    STARTER = "starter"
    STANDARD = "standard"
    GROWTH = "growth"
    ENTERPRISE = "enterprise"


class PlatformSubscription(SQLModel, table=True):
    """School platform subscription per academic term.

    Duplicate-bill protection lives at the DB level via partial unique
    indexes (created by direct migration, not create_all):
      uq_platform_sub_school_term  (school_id, academic_term_id) WHERE billing_month IS NULL
      uq_platform_sub_school_month (school_id, billing_month)    WHERE billing_month IS NOT NULL
    generate_term_subscription catches the IntegrityError these raise, so a
    race between two generation requests cannot create a double bill.
    """
    __tablename__ = "platform_subscriptions"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    academic_term_id: Optional[str] = Field(default=None, sa_column=Column(String, ForeignKey("academic_terms.id", ondelete="SET NULL"), index=True))
    
    # Billing snapshot at generation time
    student_count: int  # Active students at billing time
    unit_price: float = 400.0  # GHS per student (per school's BillingConfiguration)
    total_amount_due: float  # student_count × unit_price
    billing_plan: BillingPlan = Field(default=BillingPlan.TERMLY, sa_column=Column(String, server_default="termly"))
    billing_month: Optional[str] = Field(default=None, index=True)  # "YYYY-MM", set only for monthly-plan invoices
    
    # Payment tracking
    amount_paid: float = 0.0
    status: SubscriptionStatus = SubscriptionStatus.PENDING
    
    # Important dates
    billing_date: datetime = Field(default_factory=datetime.utcnow)
    due_date: datetime  # e.g., 30 days from billing
    paid_at: Optional[datetime] = None
    
    # Late Fees (Phase 2)
    late_fee_amount: float = 0.0  # Auto-calculated late fee
    late_fee_applied_date: Optional[datetime] = None  # When late fee was applied
    grace_period_days: int = 7  # Days after due date before late fee applies
    
    # Discounts (Phase 2)
    discount_amount: float = 0.0  # Bulk discount applied
    discount_reason: Optional[str] = None  # e.g., "500+ students"
    discount_percentage: float = 0.0  # e.g., 5.0 for 5%

    # Proration (mid-term student-count growth)
    proration_adjustment: float = 0.0  # Cumulative prorated charges for student growth after billing
    # Peak active-student count already billed/prorated for. Starts equal to
    # student_count at generation and only ever moves up — shrinkage doesn't
    # lower it (no mid-term credit is issued; see services/proration_service.py)
    # so a dip-then-recovery back to the original count is never re-charged.
    reconciled_student_count: Optional[int] = None

    # Final Amounts (Phase 2)
    subtotal: float = 0.0  # total_amount_due before discounts
    after_discount: float = 0.0  # total_amount_due - discount_amount
    final_amount_due: float = 0.0  # after_discount + late_fee_amount + proration_adjustment
    
    # Reconciliation
    invoice_id: Optional[str] = None  # Links to SubscriptionInvoice
    online_transaction_id: Optional[str] = None  # Links to OnlineTransaction
    journal_entry_id: Optional[str] = None  # GL entry for revenue
    late_fee_journal_entry_id: Optional[str] = None  # GL entry for late fee
    
    # Audit
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class SubscriptionInvoice(SQLModel, table=True):
    """Invoice for platform subscription billing"""
    __tablename__ = "subscription_invoices"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    subscription_id: str = Field(index=True)
    
    # Invoice details
    invoice_number: str = Field(unique=True, index=True)
    academic_year: str  # e.g., "2025/2026"
    term: str  # "first", "second", "third"
    
    # Billing info
    student_count: int
    unit_price: float
    subtotal: float
    tax_amount: float = 0.0  # For future tax support
    total_amount: float
    
    # Payment tracking
    amount_paid: float = 0.0
    status: str = "ISSUED"  # DRAFT, ISSUED, PARTIAL, PAID, CANCELLED
    
    # Important dates
    issued_at: datetime = Field(default_factory=datetime.utcnow)
    due_date: datetime
    paid_at: Optional[datetime] = None
    
    # Reconciliation
    journal_entry_id: Optional[str] = None  # GL entry created on payment
    notes: Optional[str] = None
    
    # Audit
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ============================================================================
# REQUEST/RESPONSE MODELS
# ============================================================================

class PlatformSubscriptionResponse(SQLModel):
    """Response model for subscription"""
    id: str
    school_id: str
    academic_term_id: str
    student_count: int
    unit_price: float
    total_amount_due: float
    amount_paid: float
    proration_adjustment: float = 0.0  # Cumulative mid-term student-growth charges
    outstanding_balance: float = 0.0  # subscription_outstanding() — what's actually owed right now
    status: SubscriptionStatus
    billing_plan: str = "termly"
    billing_month: Optional[str] = None
    billing_date: datetime
    due_date: datetime
    paid_at: Optional[datetime]
    created_at: datetime


class SubscriptionInvoiceResponse(SQLModel):
    """Response model for invoice"""
    id: str
    school_id: str
    invoice_number: str
    academic_year: str
    term: str
    student_count: int
    total_amount: float
    amount_paid: float
    status: str
    issued_at: datetime
    due_date: datetime
    paid_at: Optional[datetime]


class GenerateSubscriptionRequest(SQLModel):
    """Request to generate term subscription"""
    academic_term_id: str


class ProcessSubscriptionPaymentRequest(SQLModel):
    """Request to process subscription payment"""
    subscription_id: Optional[str] = None  # Optional - provided in path
    amount_to_pay: Optional[float] = None  # If not provided, pay full amount


class SubscriptionMetrics(SQLModel):
    """Subscription metrics for dashboard"""
    school_id: str
    current_term_status: SubscriptionStatus
    total_due: float
    total_paid: float
    remaining_balance: float
    student_count: int
    unit_price: float
    days_until_due: int
    is_overdue: bool


# ============================================================================
# PHASE 2 MODELS - LATE FEES, DISCOUNTS, REMINDERS, REPORTING
# ============================================================================

class BillingConfiguration(SQLModel, table=True):
    """Global billing configuration settings (Phase 2)"""
    __tablename__ = "billing_configurations"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True, unique=True)
    
    # Base pricing (per student). Termly is the default plan; monthly exists for
    # cash-flow flexibility and is deliberately dearer over a term (see growth playbook).
    unit_price: float = 400.0  # GHS per student per term
    monthly_unit_price: float = 150.0  # GHS per student per month (monthly plan)
    billing_plan: BillingPlan = Field(default=BillingPlan.TERMLY, sa_column=Column(String, server_default="termly"))
    grace_period_days: int = 7  # Days after due date before late fee applies
    late_fee_percentage: float = 2.5  # % of outstanding balance
    max_late_fee: Optional[float] = None  # Cap on late fee amount
    
    # Features
    enable_reminders: bool = True
    reminder_days_before_due: int = 7
    enable_late_fees: bool = True
    enable_bulk_discounts: bool = True
    
    # Special
    default_suspension_days: int = 14  # Days overdue before suspension

    # Feature tier (separate from billing_plan's cadence) — see PlanTier.
    plan_tier: PlanTier = Field(default=PlanTier.STARTER, sa_column=Column(String, server_default="starter"))

    # Saved card for auto-renewal. Populated from the `authorization` object
    # Paystack returns on a successful charge when reusable=True (see the
    # PLAT- branch of routers/payments.py's webhook handler). Auto-renewal
    # (services/scheduler.py) falls back to leaving the invoice for manual
    # checkout whenever this is unset or a charge against it fails.
    auto_renew_enabled: bool = True
    paystack_authorization_code: Optional[str] = None
    paystack_customer_code: Optional[str] = None
    paystack_card_last4: Optional[str] = None
    paystack_card_brand: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class DiscountRule(SQLModel, table=True):
    """Bulk discount rules (Phase 2)"""
    __tablename__ = "discount_rules"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    
    # Rule definition
    min_students: int  # Apply discount if student count >= this
    max_students: Optional[int] = None  # Upper limit (None = unlimited)
    discount_percentage: float  # e.g., 5.0 for 5% off
    discount_amount: Optional[float] = None  # Fixed amount instead of %
    description: str  # e.g., "500+ students get 5% off"
    
    # Status
    is_active: bool = True
    
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class PaymentReminder(SQLModel, table=True):
    """Payment reminder history (Phase 2)"""
    __tablename__ = "payment_reminders"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    subscription_id: str = Field(index=True)
    school_id: str = Field(index=True)
    
    # Reminder details
    reminder_type: str  # "sms", "email", "both"
    days_before_due: int  # How many days before due date
    message: Optional[str] = None  # Message sent
    recipient: str  # Phone or email
    
    # Status
    sent: bool = False
    sent_at: Optional[datetime] = None
    status: str = "pending"  # pending, sent, failed, bounced
    error_message: Optional[str] = None
    
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class LateFeeCharge(SQLModel, table=True):
    """Late fee tracking (Phase 2)"""
    __tablename__ = "late_fee_charges"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    subscription_id: str = Field(index=True)
    school_id: str = Field(index=True)
    
    # Fee calculation
    outstanding_balance: float  # Balance when fee was applied
    late_fee_percentage: float  # % used for calculation
    late_fee_amount: float  # Calculated fee
    max_late_fee: Optional[float]  # Cap applied
    
    # Application
    applied_date: datetime = Field(default_factory=datetime.utcnow)
    applied_by: str = "SYSTEM"  # Who applied the fee
    
    # Reconciliation
    journal_entry_id: Optional[str] = None  # GL entry for late fee

    created_at: datetime = Field(default_factory=datetime.utcnow)


class ProrationCharge(SQLModel, table=True):
    """Mid-term student-growth proration charge — append-only audit row,
    one per proration event (never updated afterwards)."""
    __tablename__ = "proration_charges"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    subscription_id: str = Field(index=True)
    school_id: str = Field(index=True)

    # Growth calculation
    previous_student_count: int  # Baseline (reconciled_student_count) before this event
    new_student_count: int  # Active student count observed at check time
    student_delta: int  # new_student_count - previous_student_count
    unit_price: float  # Rate used for this charge

    # Period this charge is prorated against
    period_start: datetime
    period_end: datetime
    period_total_days: int
    remaining_days: int  # Days from applied_date to period_end, used for the fraction

    prorated_amount: float  # student_delta * unit_price * (remaining_days / period_total_days)

    applied_date: datetime = Field(default_factory=datetime.utcnow)
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ============================================================================
# PHASE 2 RESPONSE MODELS
# ============================================================================

class BillingConfigurationResponse(SQLModel):
    """Response model for billing configuration"""
    school_id: str
    unit_price: float
    monthly_unit_price: float = 150.0
    billing_plan: str = "termly"
    grace_period_days: int
    late_fee_percentage: float
    enable_reminders: bool
    reminder_days_before_due: int
    enable_late_fees: bool
    enable_bulk_discounts: bool
    default_suspension_days: int = 14
    plan_tier: str = "starter"
    auto_renew_enabled: bool = True
    paystack_card_last4: Optional[str] = None
    paystack_card_brand: Optional[str] = None


class BillingReport(SQLModel):
    """Revenue and collection analytics (Phase 2)"""
    school_id: str
    academic_year: str
    
    # Revenue metrics
    total_subscriptions: int
    total_revenue_expected: float  # Sum of all subscription amounts
    total_revenue_collected: float  # Sum of all payments received
    collection_rate: float  # As percentage (0-100)
    
    # Aging analysis
    current_subscriptions: int  # Not overdue
    overdue_subscriptions: int  # Past due
    overdue_amount: float  # Total overdue balance
    
    # Late fees
    late_fees_charged: int  # Count of subscriptions with late fees
    late_fees_total: float  # Total late fees applied
    
    # Discounts
    total_discounts_given: float  # Sum of all discounts
    discount_rate: float  # As percentage of revenue
    
    # Payment methods
    mobile_money_count: int
    card_payment_count: int
    other_payment_count: int
