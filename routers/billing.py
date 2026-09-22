"""Billing router - handles platform subscription API endpoints"""
import logging
import os
import json
from typing import List
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse
from sqlmodel import select, SQLModel

from database import get_session
from auth import get_current_user
from models.user import User, UserRole
from models.school import AcademicTerm, School
from models.billing import (
    PlatformSubscription, SubscriptionInvoice,
    PlatformSubscriptionResponse, SubscriptionInvoiceResponse,
    GenerateSubscriptionRequest, ProcessSubscriptionPaymentRequest,
    SubscriptionMetrics, BillingConfiguration, BillingPlan, PlanTier,
    BillingConfigurationResponse
)
from typing import Optional
from models.payment import OnlineTransaction, TransactionStatus, TransactionType, PaymentVerification
from services.platform_billing_service import PlatformBillingService, mark_transaction_refunded

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])


class WaiveLateFeeRequest(SQLModel):
    reason: str = "Manual waiver"


class ReactivateSubscriptionRequest(SQLModel):
    verify_payment: bool = True


class SendRemindersRequest(SQLModel):
    school_id: Optional[str] = None


class MarkRefundedRequest(SQLModel):
    notes: Optional[str] = None
    amount: Optional[float] = None  # actual amount refunded, if it differs from the flagged amount


class CreateDiscountRuleRequest(SQLModel):
    school_id: str
    min_students: int
    discount_percentage: Optional[float] = None
    discount_amount: Optional[float] = None
    max_students: Optional[int] = None
    description: str = ""


class ToggleDiscountRuleRequest(SQLModel):
    is_active: bool



def get_billing_service() -> PlatformBillingService:
    """Initialize billing service.

    Paystack key is optional here — most billing endpoints (metrics, current
    subscription, invoices) are pure DB reads and never touch Paystack. Only
    endpoints that actually initiate/verify a payment need a real key, and
    those will fail naturally (with a clear Paystack API error) if it's missing.
    """
    paystack_secret_key = os.getenv("PAYSTACK_SECRET_KEY", "")
    return PlatformBillingService(paystack_secret_key)


# ============================================================================
# ENDPOINTS - PER-SCHOOL BILLING CONFIGURATION (super admin)
# ============================================================================

from sqlmodel import SQLModel


class UpdateBillingConfigRequest(SQLModel):
    """Super-admin request to override a school's billing configuration.

    All fields optional — only supplied fields change. Used for pilot schools
    (e.g. first term free via a 0 price) and negotiated discounts.
    """
    unit_price: Optional[float] = None          # GHS per student per term
    monthly_unit_price: Optional[float] = None  # GHS per student per month
    billing_plan: Optional[BillingPlan] = None  # termly | monthly
    plan_tier: Optional[PlanTier] = None         # starter | growth | enterprise
    # Two distinct grace periods, both counted from a subscription's
    # due_date — see services/late_fee_service.py (grace_period_days) and
    # services/subscription_suspension_service.py (default_suspension_days).
    # A school on a negotiated deal (e.g. a slow-paying but valued account)
    # can get either loosened independently — a longer suspension grace
    # doesn't require also delaying when late fees start accruing.
    grace_period_days: Optional[int] = None       # days overdue before a late fee applies
    default_suspension_days: Optional[int] = None  # days overdue before module access is suspended


class UpdateAutoRenewRequest(SQLModel):
    auto_renew_enabled: bool


class UpdatePlanTierRequest(SQLModel):
    plan_tier: PlanTier  # starter | growth | enterprise


def _config_response(config: BillingConfiguration) -> BillingConfigurationResponse:
    return BillingConfigurationResponse(
        school_id=config.school_id,
        unit_price=config.unit_price,
        monthly_unit_price=config.monthly_unit_price,
        billing_plan=config.billing_plan if isinstance(config.billing_plan, str) else config.billing_plan.value,
        grace_period_days=config.grace_period_days,
        late_fee_percentage=config.late_fee_percentage,
        enable_reminders=config.enable_reminders,
        reminder_days_before_due=config.reminder_days_before_due,
        enable_late_fees=config.enable_late_fees,
        enable_bulk_discounts=config.enable_bulk_discounts,
        default_suspension_days=config.default_suspension_days,
        plan_tier=config.plan_tier if isinstance(config.plan_tier, str) else config.plan_tier.value,
        auto_renew_enabled=config.auto_renew_enabled,
        paystack_card_last4=config.paystack_card_last4,
        paystack_card_brand=config.paystack_card_brand,
    )


@router.get("/config/{school_id}", response_model=BillingConfigurationResponse)
async def get_billing_config(
    school_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Get a school's billing configuration (super admin, or the school's own admin read-only)."""
    if current_user.role not in (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN):
        raise HTTPException(status_code=403, detail="Not authorised")
    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != school_id:
        raise HTTPException(status_code=403, detail="Access denied")

    result = await session.execute(
        select(BillingConfiguration).where(BillingConfiguration.school_id == school_id)
    )
    config = result.scalar_one_or_none()
    if not config:
        config = BillingConfiguration(school_id=school_id)
        session.add(config)
        await session.commit()
        await session.refresh(config)
    return _config_response(config)


@router.put("/config/{school_id}", response_model=BillingConfigurationResponse)
async def update_billing_config(
    school_id: str,
    request_data: UpdateBillingConfigRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Override a school's pricing/plan (super admin only)."""
    if current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Super admin only")

    result = await session.execute(
        select(BillingConfiguration).where(BillingConfiguration.school_id == school_id)
    )
    config = result.scalar_one_or_none()
    if not config:
        config = BillingConfiguration(school_id=school_id)
        session.add(config)

    if request_data.unit_price is not None:
        if request_data.unit_price < 0:
            raise HTTPException(status_code=400, detail="unit_price cannot be negative")
        config.unit_price = request_data.unit_price
    if request_data.monthly_unit_price is not None:
        if request_data.monthly_unit_price < 0:
            raise HTTPException(status_code=400, detail="monthly_unit_price cannot be negative")
        config.monthly_unit_price = request_data.monthly_unit_price
    if request_data.billing_plan is not None:
        config.billing_plan = request_data.billing_plan.value
    if request_data.plan_tier is not None:
        config.plan_tier = request_data.plan_tier.value
    if request_data.grace_period_days is not None:
        if request_data.grace_period_days < 0:
            raise HTTPException(status_code=400, detail="grace_period_days cannot be negative")
        config.grace_period_days = request_data.grace_period_days
    if request_data.default_suspension_days is not None:
        if request_data.default_suspension_days < 0:
            raise HTTPException(status_code=400, detail="default_suspension_days cannot be negative")
        config.default_suspension_days = request_data.default_suspension_days

    config.updated_at = datetime.utcnow()
    session.add(config)
    await session.commit()
    await session.refresh(config)

    logger.info(
        f"Billing config updated for school {school_id} by {current_user.id}: "
        f"unit_price={config.unit_price}, monthly={config.monthly_unit_price}, plan={config.billing_plan}, "
        f"tier={config.plan_tier}, grace_period_days={config.grace_period_days}, "
        f"default_suspension_days={config.default_suspension_days}"
    )
    return _config_response(config)


@router.put("/config/{school_id}/auto-renew", response_model=BillingConfigurationResponse)
async def update_auto_renew(
    school_id: str,
    request_data: UpdateAutoRenewRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Let a school turn auto-renewal on/off for itself — unlike the rest of
    /config, this one field is the subscriber's own choice, not a super-admin
    override. Turning it off doesn't clear the saved card, only stops the
    scheduler from charging it (see services/scheduler.py)."""
    if current_user.role == UserRole.SUPER_ADMIN:
        pass
    elif current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id == school_id:
        pass
    else:
        raise HTTPException(status_code=403, detail="Access denied")

    result = await session.execute(
        select(BillingConfiguration).where(BillingConfiguration.school_id == school_id)
    )
    config = result.scalar_one_or_none()
    if not config:
        config = BillingConfiguration(school_id=school_id)
        session.add(config)

    config.auto_renew_enabled = request_data.auto_renew_enabled
    config.updated_at = datetime.utcnow()
    session.add(config)
    await session.commit()
    await session.refresh(config)
    return _config_response(config)


@router.put("/config/{school_id}/plan-tier", response_model=BillingConfigurationResponse)
async def update_plan_tier(
    school_id: str,
    request_data: UpdatePlanTierRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Let a school switch its own subscription plan tier — like auto-renew,
    this is the subscriber's own choice, not a super-admin override. Pricing
    (unit_price/monthly_unit_price) and grace periods stay super-admin-only
    via PUT /config/{school_id} and are NOT affected by this endpoint: a
    tier change here does not itself change what the school is charged,
    since price and tier are independent fields on BillingConfiguration."""
    if current_user.role == UserRole.SUPER_ADMIN:
        pass
    elif current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id == school_id:
        pass
    else:
        raise HTTPException(status_code=403, detail="Access denied")

    result = await session.execute(
        select(BillingConfiguration).where(BillingConfiguration.school_id == school_id)
    )
    config = result.scalar_one_or_none()
    if not config:
        config = BillingConfiguration(school_id=school_id)
        session.add(config)

    config.plan_tier = request_data.plan_tier.value
    config.updated_at = datetime.utcnow()
    session.add(config)
    await session.commit()
    await session.refresh(config)

    logger.info(
        f"Plan tier for school {school_id} changed to {config.plan_tier} by {current_user.id} "
        f"(role={current_user.role})"
    )
    return _config_response(config)


# ============================================================================
# ENDPOINTS - SUBSCRIPTION MANAGEMENT
# ============================================================================

@router.post("/subscriptions/generate-term", status_code=201)
async def generate_term_subscription(
    request_data: GenerateSubscriptionRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    billing_service: PlatformBillingService = Depends(get_billing_service)
) -> dict:
    """
    Generate platform subscription for academic term
    
    **Auth Required:** Admin, Principal, or School roles
    
    **Request Body:**
    ```json
    {
        "academic_term_id": "term-uuid"
    }
    ```
    
    **Response:**
    ```json
    {
        "success": true,
        "subscription_id": "sub-xxx",
        "invoice_id": "inv-xxx",
        "student_count": 420,
        "total_due": 168000.00,
        "due_date": "2026-05-15T10:30:00"
    }
    ```
    """
    
    # Verify authorization
    if current_user.role not in [UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins or school users can generate subscriptions"
        )
    
    if current_user.role == UserRole.SCHOOL_ADMIN:
        school_id = current_user.school_id
    else:
        # Admin must be with a school
        if not current_user.school_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Admin must be associated with a school"
            )
        school_id = current_user.school_id
    
    result = await billing_service.generate_term_subscription(
        session=session,
        school_id=school_id,
        academic_term_id=request_data.academic_term_id
    )
    
    if not result.get("success"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result.get("error", "Failed to generate subscription")
        )
    
    return result


@router.get("/subscriptions/current", status_code=200)
async def get_current_subscription(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    billing_service: PlatformBillingService = Depends(get_billing_service)
) -> dict:
    """
    Get current academic term subscription for school
    
    **Auth Required:** Authenticated user
    
    **Response:**
    ```json
    {
        "id": "sub-xxx",
        "school_id": "school-xxx",
        "academic_term_id": "term-xxx",
        "student_count": 420,
        "unit_price": 400.00,
        "total_amount_due": 168000.00,
        "amount_paid": 0.00,
        "status": "pending",
        "billing_date": "2026-04-15T10:30:00",
        "due_date": "2026-05-15T10:30:00",
        "paid_at": null,
        "created_at": "2026-04-15T10:30:00"
    }
    ```
    """
    
    if not current_user.school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User must be associated with a school"
        )
    
    subscription = await billing_service.get_school_current_subscription(
        session=session,
        school_id=current_user.school_id
    )
    
    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active subscription found"
        )
    
    return subscription.dict()


@router.get("/subscriptions/{subscription_id}", status_code=200)
async def get_subscription(
    subscription_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    billing_service: PlatformBillingService = Depends(get_billing_service)
) -> dict:
    """
    Get subscription details by ID
    
    **Auth Required:** Authenticated user
    """
    
    subscription = await billing_service.get_subscription(
        session=session,
        subscription_id=subscription_id
    )
    
    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subscription not found"
        )
    
    # Verify access
    if current_user.role == UserRole.SCHOOL_ADMIN and subscription.school_id != current_user.school_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied"
        )
    
    return subscription.dict()


@router.get("/subscriptions", status_code=200)
async def get_school_subscriptions(
    limit: int = 10,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    billing_service: PlatformBillingService = Depends(get_billing_service)
) -> dict:
    """
    Get all subscriptions for school (paginated)
    
    **Auth Required:** Authenticated user
    
    **Query Parameters:**
    - `limit`: Number of subscriptions per page (default: 10)
    - `offset`: Pagination offset (default: 0)
    """
    
    if not current_user.school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User must be associated with a school"
        )
    
    subscriptions = await billing_service.get_school_subscriptions(
        session=session,
        school_id=current_user.school_id,
        limit=limit,
        offset=offset
    )
    
    return {
        "count": len(subscriptions),
        "limit": limit,
        "offset": offset,
        "data": [sub.dict() for sub in subscriptions]
    }


# ============================================================================
# ENDPOINTS - INVOICES
# ============================================================================

@router.get("/invoices", status_code=200)
async def get_school_invoices(
    limit: int = 10,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    billing_service: PlatformBillingService = Depends(get_billing_service)
) -> dict:
    """
    Get all invoices for school (paginated)
    
    **Auth Required:** Authenticated user
    
    **Query Parameters:**
    - `limit`: Number of invoices per page (default: 10)
    - `offset`: Pagination offset (default: 0)
    """
    
    if not current_user.school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User must be associated with a school"
        )
    
    invoices = await billing_service.get_school_invoices(
        session=session,
        school_id=current_user.school_id,
        limit=limit,
        offset=offset
    )
    
    return {
        "count": len(invoices),
        "limit": limit,
        "offset": offset,
        "data": [inv.dict() for inv in invoices]
    }


@router.get("/invoices/{invoice_id}/download", status_code=200)
async def download_invoice_pdf(
    invoice_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    billing_service: PlatformBillingService = Depends(get_billing_service)
):
    """
    Download invoice as PDF
    
    **Auth Required:** Authenticated user
    
    **Path Parameters:**
    - invoice_id: UUID of the invoice
    
    **Returns:**
    PDF file as attachment
    """
    from services.invoice_pdf_service import InvoicePDFService
    from models.billing import SubscriptionInvoice
    from starlette.responses import StreamingResponse
    
    try:
        if not current_user.school_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User must be associated with a school"
            )
        
        # Get invoice
        invoice_result = await session.execute(
            select(SubscriptionInvoice).where(
                SubscriptionInvoice.id == invoice_id,
                SubscriptionInvoice.school_id == current_user.school_id
            )
        )
        invoice = invoice_result.scalar_one_or_none()
        
        if not invoice:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Invoice not found"
            )
        
        # Get school name
        from models.school import School
        school_result = await session.execute(
            select(School).where(School.id == current_user.school_id)
        )
        school = school_result.scalar_one_or_none()
        school_name = school.name if school else "School"
        
        # Convert invoice to dict
        invoice_dict = {
            "id": invoice.id,
            "invoice_number": invoice.invoice_number,
            "academic_year": invoice.academic_year,
            "term": invoice.term,
            "student_count": invoice.student_count,
            "unit_price": float(invoice.unit_price),
            "subtotal": float(invoice.subtotal),
            "tax_amount": float(invoice.tax_amount),
            "total_amount": float(invoice.total_amount),
            "amount_paid": float(invoice.amount_paid),
            "status": invoice.status,
            "issued_at": invoice.issued_at,
            "due_date": invoice.due_date,
            "paid_at": invoice.paid_at
        }
        
        # Generate PDF
        pdf_service = InvoicePDFService()
        pdf_bytes = pdf_service.generate_pdf(invoice_dict, school_name)
        
        if not pdf_bytes or len(pdf_bytes) == 0:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to generate PDF"
            )
        
        # Return as downloadable PDF
        filename = f"invoice_{invoice.invoice_number}.pdf"
        return StreamingResponse(
            iter([pdf_bytes]),
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating invoice PDF: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate PDF: {str(e)}"
        )


# ============================================================================
# ENDPOINTS - PAYMENT PROCESSING
# ============================================================================

@router.post("/subscriptions/{subscription_id}/pay", status_code=200)
async def initiate_subscription_payment(
    subscription_id: str,
    request_data: ProcessSubscriptionPaymentRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    billing_service: PlatformBillingService = Depends(get_billing_service)
) -> dict:
    """
    Initiate payment for platform subscription via Paystack
    
    **Auth Required:** Authenticated user
    
    **Request Body:**
    ```json
    {
        "subscription_id": "sub-xxx",
        "amount_to_pay": 168000.00  // Optional, defaults to full amount
    }
    ```
    
    **Response:**
    ```json
    {
        "success": true,
        "transaction_id": "txn-xxx",
        "payment_url": "https://checkout.paystack.com/...",
        "reference": "PLAT-xxx",
        "amount": 168000.00
    }
    ```
    """
    
    if not current_user.school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User must be associated with a school"
        )
    
    # Get subscription to verify access
    subscription = await billing_service.get_subscription(
        session=session,
        subscription_id=subscription_id
    )
    
    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subscription not found"
        )
    
    if subscription.school_id != current_user.school_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied"
        )
    
    # Initiate payment
    result = await billing_service.initiate_subscription_payment(
        session=session,
        subscription_id=subscription_id,
        payer_email=current_user.email,
        amount_to_pay=request_data.amount_to_pay
    )
    
    if not result.get("success"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result.get("error", "Failed to initiate payment")
        )
    
    return result


@router.get("/subscriptions/{subscription_id}/payment-status/{transaction_id}", status_code=200)
async def get_subscription_payment_status(
    subscription_id: str,
    transaction_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Get payment status for a subscription payment transaction
    
    **Auth Required:** Authenticated user
    
    **Path Parameters:**
    - subscription_id: UUID of the subscription
    - transaction_id: UUID of the transaction (from payment initialization)
    
    **Response:**
    ```json
    {
        "status": "pending|processing|success|failed",
        "message": "Payment status",
        "amount": 168000.00,
        "reference": "PLAT-xxx",
        "updated_at": "2026-04-16T10:30:00"
    }
    ```
    
    **Errors:**
    - 404: Transaction or subscription not found
    - 403: Not authorized to view this transaction
    """
    
    try:
        if not current_user.school_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User must be associated with a school"
            )
        
        # Get subscription to verify access and fee_id
        sub_result = await session.execute(
            select(PlatformSubscription).where(
                PlatformSubscription.id == subscription_id
            )
        )
        subscription = sub_result.scalar_one_or_none()
        
        if not subscription:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Subscription not found"
            )
        
        # Verify school access
        if subscription.school_id != current_user.school_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to view this subscription"
            )
        
        # Get transaction by database ID
        # transaction_id here is the database UUID returned from payment initialization
        txn_result = await session.execute(
            select(OnlineTransaction).where(
                OnlineTransaction.id == transaction_id
            )
        )
        transaction = txn_result.scalar_one_or_none()
        
        if not transaction:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Transaction not found"
            )
        
        # Verify transaction belongs to this subscription
        if transaction.fee_id != subscription_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Transaction does not belong to this subscription"
            )
        
        # Build response based on transaction status
        response = {
            "status": transaction.status.value,
            "message": "",
            "amount": float(transaction.amount),
            "reference": transaction.reference,
            "updated_at": transaction.updated_at.isoformat() if transaction.updated_at else transaction.created_at.isoformat()
        }
        
        if transaction.status == TransactionStatus.SUCCESS:
            response["message"] = "Payment successful"
            response["completed_at"] = transaction.completed_at.isoformat() if transaction.completed_at else None
        elif transaction.status == TransactionStatus.FAILED:
            response["message"] = transaction.failed_reason or "Payment failed"
        elif transaction.status == TransactionStatus.PROCESSING:
            response["message"] = "Payment processing"
        else:  # PENDING
            response["message"] = "Payment pending - waiting for confirmation"
        
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting subscription payment status: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error retrieving payment status"
        )


# ============================================================================
# ENDPOINTS - DASHBOARDS & METRICS
# ============================================================================

@router.get("/metrics", status_code=200)
async def get_subscription_metrics(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    billing_service: PlatformBillingService = Depends(get_billing_service)
) -> dict:
    """
    Get subscription metrics for school dashboard
    
    **Auth Required:** Authenticated user
    
    **Auto-generates subscription** if none exists for current term
    
    **Response:**
    ```json
    {
        "school_id": "school-xxx",
        "current_term_status": "pending",
        "total_due": 168000.00,
        "total_paid": 0.00,
        "remaining_balance": 168000.00,
        "student_count": 420,
        "unit_price": 400.00,
        "days_until_due": 30,
        "is_overdue": false
    }
    ```
    """
    
    if not current_user.school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User must be associated with a school"
        )
    
    # Try to get existing metrics
    metrics = await billing_service.get_subscription_metrics(
        session=session,
        school_id=current_user.school_id
    )
    
    # If no metrics, try to auto-generate subscription for current term
    if not metrics:
        # Get current academic term
        term_result = await session.execute(
            select(AcademicTerm)
            .where(AcademicTerm.school_id == current_user.school_id)
            .order_by(AcademicTerm.start_date.desc())
        )
        current_term = term_result.scalars().first()
        
        if current_term:
            # Try to generate subscription
            result = await billing_service.generate_term_subscription(
                session=session,
                school_id=current_user.school_id,
                academic_term_id=current_term.id
            )
            
            if result.get("success"):
                # Retry getting metrics
                metrics = await billing_service.get_subscription_metrics(
                    session=session,
                    school_id=current_user.school_id
                )
        
        if not metrics:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No subscription available. Please ensure students are enrolled and try again."
            )
    
    return metrics.dict()


# ============================================================================
# ENDPOINTS - PHASE 2: LATE FEES, DISCOUNTS, REMINDERS, REPORTING
# ============================================================================

# --- LATE FEES ENDPOINTS ---

@router.get("/subscriptions/overdue/list", status_code=200)
async def get_overdue_subscriptions(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    List subscriptions past due with an outstanding balance (Admin only).

    Data source for the late-fee admin dashboard — lists real subscriptions
    with subscription_id, invoice_number, days_overdue, and the actual
    outstanding balance (late fees/discounts included), across all schools
    for a super admin.

    **Auth Required:** Admin

    **Response:**
    ```json
    {
        "count": 3,
        "subscriptions": [
            {
                "subscription_id": "sub-xxx",
                "school_id": "school-xxx",
                "invoice_number": "PLAT-2026-0004",
                "due_date": "2026-06-15T10:30:00",
                "days_overdue": 12,
                "total_amount_due": 168000.00,
                "amount_paid": 0.00,
                "late_fee_amount": 0.00,
                "outstanding": 168000.00,
                "status": "pending"
            }
        ]
    }
    ```
    """
    if current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can view overdue subscriptions"
        )

    from services.late_fee_service import LateFeeService

    service = LateFeeService()
    subscriptions = await service.get_overdue_subscriptions(session=session, school_id=None)

    return {"count": len(subscriptions), "subscriptions": subscriptions}


@router.post("/late-fees/apply", status_code=200)
async def apply_late_fees(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Check and apply late fees to overdue subscriptions (Admin only)
    
    **Auth Required:** Admin
    
    **Response:**
    ```json
    {
        "success": true,
        "subscriptions_with_late_fees": 5,
        "message": "Applied late fees to 5 subscriptions"
    }
    ```
    """
    
    # Admin only
    if current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can apply late fees"
        )
    
    from services.late_fee_service import LateFeeService

    service = LateFeeService()
    result = await service.check_and_apply_late_fees(
        session=session,
        school_id=current_user.school_id if current_user.school_id else None
    )

    return result


# --- PRORATION ENDPOINTS ---

@router.get("/subscriptions/proration/list", status_code=200)
async def list_proration_overview(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    List subscriptions with an active billing period, showing billed vs
    live student counts and a preview of any pending (uncharged) proration
    (Admin only). Data source for the proration admin dashboard.

    **Auth Required:** Admin

    **Response:**
    ```json
    {
        "count": 1,
        "subscriptions": [
            {
                "subscription_id": "sub-xxx",
                "school_id": "school-xxx",
                "status": "active",
                "billed_student_count": 420,
                "reconciled_student_count": 420,
                "current_student_count": 435,
                "pending_delta": 15,
                "pending_charge_preview": 2600.00,
                "already_charged": 0.0,
                "period_end": "2026-11-30T00:00:00",
                "remaining_days": 13
            }
        ]
    }
    ```
    """
    if current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can view proration overview"
        )

    from services.proration_service import ProrationService

    service = ProrationService()
    subscriptions = await service.list_subscriptions_overview(session=session, school_id=None)

    return {"count": len(subscriptions), "subscriptions": subscriptions}


@router.post("/proration/apply", status_code=200)
async def apply_proration(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Check subscriptions for mid-term student-count growth and charge a
    prorated amount for the delta (Admin only). Shrinkage issues no credit —
    see services/proration_service.py for why.

    **Auth Required:** Admin

    **Response:**
    ```json
    {
        "success": true,
        "subscriptions_prorated": 2,
        "total_charged": 3200.00,
        "message": "Applied proration charges to 2 subscriptions"
    }
    ```
    """
    if current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can apply proration"
        )

    from services.proration_service import ProrationService

    service = ProrationService()
    result = await service.check_and_apply_proration(
        session=session,
        school_id=current_user.school_id if current_user.school_id else None
    )

    return result


@router.get("/subscriptions/{subscription_id}/proration-history", status_code=200)
async def get_proration_history(
    subscription_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Get proration charge history for a subscription.

    **Auth Required:** Authenticated user (school admin scoped to their own
    subscription, super admin unrestricted)
    """
    sub_result = await session.execute(
        select(PlatformSubscription).where(PlatformSubscription.id == subscription_id)
    )
    subscription = sub_result.scalar_one_or_none()
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription not found")

    if current_user.role == UserRole.SCHOOL_ADMIN and subscription.school_id != current_user.school_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    from services.proration_service import ProrationService

    service = ProrationService()
    history = await service.get_proration_history(session=session, subscription_id=subscription_id)

    return {"subscription_id": subscription_id, "count": len(history), "charges": history}


@router.post("/subscriptions/{subscription_id}/late-fee/waive", status_code=200)
async def waive_late_fee(
    subscription_id: str,
    body: WaiveLateFeeRequest = WaiveLateFeeRequest(),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Waive late fee for subscription (Admin only)
    
    **Auth Required:** Admin
    
    **Response:**
    ```json
    {
        "success": true,
        "waived_amount": 420.00,
        "new_total_due": 168000.00
    }
    ```
    """
    reason = body.reason
    
    # Admin or school admin
    if current_user.role not in [UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized"
        )
    
    from services.late_fee_service import LateFeeService
    
    service = LateFeeService()
    result = await service.waive_late_fee(
        session=session,
        subscription_id=subscription_id,
        reason=reason
    )
    
    return result


# --- SUSPENSION ENDPOINTS ---

@router.post("/subscriptions/suspend/check-overdue", status_code=200)
async def check_and_suspend_overdue(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Check for overdue subscriptions and suspend if past suspension period (Admin only)
    
    **Auth Required:** Admin
    
    **Response:**
    ```json
    {
        "success": true,
        "subscriptions_suspended": 2,
        "message": "Suspended 2 overdue subscriptions"
    }
    ```
    """
    
    if current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    
    from services.subscription_suspension_service import SubscriptionSuspensionService
    
    service = SubscriptionSuspensionService()
    
    # Check all school suspensions
    all_results = []
    result = await service.check_and_suspend_overdue(
        session=session,
        school_id=None  # Check all schools
    )
    
    return result


@router.post("/subscriptions/{subscription_id}/suspend", status_code=200)
async def suspend_subscription(
    subscription_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    billing_service: PlatformBillingService = Depends(get_billing_service)
) -> dict:
    """
    Manually suspend a subscription (Admin only)
    
    **Auth Required:** Admin
    
    **Response:**
    ```json
    {
        "success": true,
        "message": "Subscription suspended",
        "subscription_id": "sub-xxx"
    }
    ```
    """
    
    if current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    
    from services.subscription_suspension_service import SubscriptionSuspensionService
    
    service = SubscriptionSuspensionService()
    result = await service.suspend_subscription(
        session=session,
        subscription_id=subscription_id
    )
    
    return result


@router.post("/subscriptions/{subscription_id}/reactivate", status_code=200)
async def reactivate_subscription(
    subscription_id: str,
    body: ReactivateSubscriptionRequest = ReactivateSubscriptionRequest(),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Reactivate a suspended subscription (Admin only)
    
    **Auth Required:** Admin or School
    
    **Query Params:**
    - `verify_payment`: If true, verify payment is complete before reactivating
    
    **Response:**
    ```json
    {
        "success": true,
        "message": "Subscription reactivated",
        "subscription_id": "sub-xxx"
    }
    ```
    """
    verify_payment = body.verify_payment
    
    if current_user.role not in [UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin or school access required"
        )
    
    # Verify access
    subscription = await session.execute(
        select(PlatformSubscription).where(
            PlatformSubscription.id == subscription_id
        )
    )
    sub = subscription.scalar_one_or_none()
    
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subscription not found"
        )
    
    if current_user.role == UserRole.SCHOOL_ADMIN and sub.school_id != current_user.school_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied"
        )
    
    from services.subscription_suspension_service import SubscriptionSuspensionService
    
    service = SubscriptionSuspensionService()
    result = await service.reactivate_subscription(
        session=session,
        subscription_id=subscription_id,
        verify_payment=verify_payment
    )
    
    return result


@router.get("/subscriptions/suspended/list", status_code=200)
async def get_suspended_subscriptions(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Get all suspended subscriptions for school (Admin only)
    
    **Auth Required:** Admin or School
    
    **Response:**
    ```json
    {
        "suspended_count": 2,
        "subscriptions": [...]
    }
    ```
    """
    
    if current_user.role == UserRole.SCHOOL_ADMIN and not current_user.school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User must be associated with a school"
        )
    
    from services.subscription_suspension_service import SubscriptionSuspensionService
    
    service = SubscriptionSuspensionService()
    
    school_id = current_user.school_id if current_user.role == UserRole.SCHOOL_ADMIN else None
    
    suspended = await service.get_suspended_subscriptions(
        session=session,
        school_id=school_id
    )
    
    return {
        "suspended_count": len(suspended),
        "subscriptions": suspended
    }


# --- REFUND ENDPOINTS ---
# Platform-subscription overpayments only (Campusio's money). School-fee
# overpayments (parent-facing) are handled the same way under
# /payments/refunds — see routers/payments.py.

@router.get("/refunds/pending", status_code=200)
async def get_pending_subscription_refunds(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    List platform subscription transactions flagged for refund
    (overpayments that arrived but couldn't be applied to any
    outstanding balance). Super admin only — this is Campusio's money.
    """
    if current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")

    result = await session.execute(
        select(OnlineTransaction).where(
            OnlineTransaction.transaction_type == TransactionType.SUBSCRIPTION,
            OnlineTransaction.refund_status == "pending"
        ).order_by(OnlineTransaction.completed_at.desc())
    )
    transactions = result.scalars().all()

    return {
        "count": len(transactions),
        "total_refund_amount": round(sum(t.refund_amount or 0 for t in transactions), 2),
        "refunds": [
            {
                "transaction_id": t.id,
                "school_id": t.school_id,
                "reference": t.reference,
                "amount_paid": t.amount_paid,
                "refund_amount": t.refund_amount,
                "reason": t.failed_reason,
                "completed_at": t.completed_at.isoformat() if t.completed_at else None,
            }
            for t in transactions
        ]
    }


@router.post("/refunds/{transaction_id}/mark-refunded", status_code=200)
async def mark_subscription_refund_completed(
    transaction_id: str,
    body: MarkRefundedRequest = MarkRefundedRequest(),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Mark a flagged subscription overpayment as refunded. Does not call a
    live payment-gateway refund API — this closes out the manual refund
    queue entry once the school has actually been paid back (e.g. bank
    transfer), the same manual-reconciliation pattern used elsewhere in
    this system.
    """
    if current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")

    result = await mark_transaction_refunded(
        session=session,
        transaction_id=transaction_id,
        refunded_by=current_user.id,
        notes=body.notes,
        amount=body.amount,
    )

    if not result.get("success"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result.get("error"))

    return result


# --- REMINDER ENDPOINTS ---

@router.post("/reminders/send-pending", status_code=200)
async def send_pending_reminders(
    body: SendRemindersRequest = SendRemindersRequest(),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Send pending payment reminders (Admin only)
    
    Sends reminders for:
    - 7 days before due date
    - On due date
    - 3 days after due date (overdue)
    
    **Auth Required:** Admin
    
    **Response:**
    ```json
    {
        "success": true,
        "reminders_sent": 5,
        "message": "Sent 5 payment reminders"
    }
    ```
    """
    school_id = body.school_id
    
    if current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    
    from services.payment_reminder_service import PaymentReminderService
    
    service = PaymentReminderService()
    result = await service.send_pending_reminders(
        session=session,
        school_id=school_id
    )

    return result


@router.get("/reminders/{subscription_id}/history", status_code=200)
async def get_reminder_history(
    subscription_id: str,
    limit: int = 10,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Get reminder history for a subscription
    
    **Auth Required:** Authenticated user
    
    **Response:**
    ```json
    {
        "subscription_id": "sub-xxx",
        "reminder_count": 3,
        "reminders": [...]
    }
    ```
    """
    
    from services.payment_reminder_service import PaymentReminderService
    
    service = PaymentReminderService()
    result = await service.get_reminder_history(
        session=session,
        subscription_id=subscription_id,
        limit=limit
    )

    return {
        "subscription_id": subscription_id,
        "reminder_count": result.get("count", 0),
        "reminders": result.get("data", [])
    }


# --- DISCOUNT ENDPOINTS ---

@router.post("/discounts/rules", status_code=201)
async def create_discount_rule(
    body: CreateDiscountRuleRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Create a bulk discount rule for a school (super admin only).

    This grants a school a discount on their platform subscription — it must
    never be self-service. A school_admin creating a discount rule for their
    own school would be granting themselves a discount on what they owe us.

    **Response:**
    ```json
    {
        "success": true,
        "rule_id": "rule-xxx",
        "description": "500+ students get 5% off"
    }
    ```
    """
    if current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only super admins can create discount rules"
        )

    from services.bulk_discount_service import BulkDiscountService

    service = BulkDiscountService()
    result = await service.create_discount_rule(
        session=session,
        school_id=body.school_id,
        min_students=body.min_students,
        discount_percentage=body.discount_percentage,
        discount_amount=body.discount_amount,
        max_students=body.max_students,
        description=body.description
    )

    return result


@router.get("/discounts/rules", status_code=200)
async def list_discount_rules(
    school_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    List discount rules for a school. Super admin may pass any school_id
    (they have none of their own); a school admin always sees their own
    school's rules regardless of what's passed, so they can't probe another
    school's negotiated pricing.

    **Response:**
    ```json
    {
        "rules": [
            {
                "id": "rule-xxx",
                "min_students": 500,
                "discount_percentage": 5.0,
                "description": "500+ students get 5% off"
            }
        ]
    }
    ```
    """
    if current_user.role == UserRole.SUPER_ADMIN:
        if not school_id:
            raise HTTPException(status_code=400, detail="school_id is required")
        target_school_id = school_id
    else:
        if not current_user.school_id:
            raise HTTPException(status_code=400, detail="User must be associated with a school")
        target_school_id = current_user.school_id

    from services.bulk_discount_service import BulkDiscountService

    service = BulkDiscountService()
    rules = await service.list_discount_rules(
        session=session,
        school_id=target_school_id
    )

    return {"rules": rules}


@router.put("/discounts/rules/{rule_id}/toggle", status_code=200)
async def toggle_discount_rule(
    rule_id: str,
    body: ToggleDiscountRuleRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """Enable or disable a discount rule (super admin only)."""
    if current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only super admins can toggle discount rules"
        )

    from services.bulk_discount_service import BulkDiscountService

    service = BulkDiscountService()
    result = await service.toggle_rule(session=session, rule_id=rule_id, is_active=body.is_active)

    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error", "Rule not found"))

    return result


# --- REMINDER ENDPOINTS ---
# (POST /reminders/send-pending is defined once, earlier in this file — a
# second definition of the same route used to live here and was dead code:
# FastAPI matches the first registration, so the duplicate never ran.)

@router.get("/subscriptions/{subscription_id}/reminders", status_code=200)
async def get_subscription_reminders(
    subscription_id: str,
    limit: int = 10,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Get reminder history for subscription
    
    **Auth Required:** Authenticated user
    
    **Response:**
    ```json
    {
        "count": 3,
        "data": [
            {
                "id": "rem-xxx",
                "type": "sms",
                "recipient": "+233XXXXXXXXXX",
                "status": "sent",
                "sent_at": "2026-05-08T10:30:00"
            }
        ]
    }
    ```
    """
    
    from services.payment_reminder_service import PaymentReminderService
    
    service = PaymentReminderService()
    result = await service.get_reminder_history(
        session=session,
        subscription_id=subscription_id,
        limit=limit,
        offset=offset
    )
    
    return result


# --- REPORTING ENDPOINTS ---

@router.get("/reports/revenue", status_code=200)
async def get_revenue_report(
    school_id: str = None,
    academic_year: str = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Get revenue and collection report
    
    **Auth Required:** Admin
    
    **Query Params:**
    - `school_id` (optional): Filter by school
    - `academic_year` (optional): Filter by year (e.g., "2025/2026")
    
    **Response:**
    ```json
    {
        "success": true,
        "report": {
            "school_id": "ALL",
            "academic_year": "2025/2026",
            "total_subscriptions": 42,
            "total_revenue_expected": 352800.00,
            "total_revenue_collected": 298600.00,
            "collection_rate": 84.64,
            "overdue_subscriptions": 8,
            "overdue_amount": 67200.00,
            "late_fees_charged": 5,
            "late_fees_total": 2100.00,
            "total_discounts_given": 28000.00
        }
    }
    ```
    """
    
    if current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can view reports"
        )
    
    # If not admin of all schools, can only view own school
    if school_id and school_id != current_user.school_id:
        if current_user.role != UserRole.SUPER_ADMIN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot view other schools' reports"
            )
    
    from services.billing_reporting_service import BillingReportingService
    
    service = BillingReportingService()
    result = await service.get_revenue_report(
        session=session,
        school_id=school_id,
        academic_year=academic_year
    )
    
    return result


@router.get("/reports/aging", status_code=200)
async def get_aging_analysis(
    school_id: str = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Get aging analysis of overdue accounts
    
    **Auth Required:** Admin
    
    **Response:**
    ```json
    {
        "success": true,
        "total_overdue": 67200.00,
        "aging_buckets": {
            "current": {"count": 34, "amount": 285200.00, "percentage": 80.92},
            "1_30_days": {"count": 5, "amount": 42000.00, "percentage": 11.91},
            "31_60_days": {"count": 2, "amount": 16800.00, "percentage": 4.76},
            "61_plus_days": {"count": 1, "amount": 168000.00, "percentage": 2.38}
        }
    }
    ```
    """
    
    if current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can view reports"
        )
    
    from services.billing_reporting_service import BillingReportingService
    
    service = BillingReportingService()
    result = await service.get_aging_analysis(
        session=session,
        school_id=school_id
    )
    
    return result


@router.get("/reports/top-paying-schools", status_code=200)
async def get_top_paying_schools(
    limit: int = 10,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Get top paying schools by collection rate (System Admin only)
    
    **Auth Required:** Admin
    
    **Response:**
    ```json
    {
        "schools": [
            {
                "school_id": "sch-001",
                "collection_rate": 98.50,
                "expected": 84000.00,
                "collected": 82800.00
            }
        ]
    }
    ```
    """
    
    if current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can view reports"
        )
    
    from services.billing_reporting_service import BillingReportingService
    
    service = BillingReportingService()
    schools = await service.get_top_paying_schools(
        session=session,
        limit=limit
    )
    
    return {"schools": schools}


@router.get("/reports/bottom-paying-schools", status_code=200)
async def get_bottom_paying_schools(
    limit: int = 10,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Get bottom paying schools needing follow-up (System Admin only)
    
    **Auth Required:** Admin
    
    **Response:**
    ```json
    {
        "schools": [
            {
                "school_id": "sch-042",
                "collection_rate": 42.30,
                "expected": 84000.00,
                "collected": 35532.00,
                "overdue": 28000.00,
                "late_fees": 1400.00
            }
        ]
    }
    ```
    """
    
    if current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can view reports"
        )
    
    from services.billing_reporting_service import BillingReportingService
    
    service = BillingReportingService()
    schools = await service.get_bottom_paying_schools(
        session=session,
        limit=limit
    )
    
    return {"schools": schools}
