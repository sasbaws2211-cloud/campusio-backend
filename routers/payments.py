"""Payment router - handles payment API endpoints"""
import logging
import json
from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import and_
from sqlmodel import select
from pydantic import BaseModel
import os

from database import get_session
from auth import get_current_user
from models.payment import OnlineTransaction, TransactionStatus, TransactionType
from models.user import User, UserRole
from models.fee import Fee
from models.student import Student, Parent, StudentParent
from models.billing import BillingConfiguration
from services.plan_gating import require_plan_feature

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["payments"])


async def _save_paystack_authorization(session: AsyncSession, school_id: str, charge_data: dict) -> None:
    """Save a reusable card authorization from a successful platform
    subscription charge, so services/scheduler.py can auto-charge future
    renewals instead of the school having to remember to pay each cycle.
    Only ever called for charge.success on a PLAT- reference — never for
    school-fee payments, which have nothing to do with the school's own
    platform subscription."""
    authorization = charge_data.get("authorization") or {}
    if not authorization.get("reusable") or not authorization.get("authorization_code"):
        return

    result = await session.execute(
        select(BillingConfiguration).where(BillingConfiguration.school_id == school_id)
    )
    config = result.scalar_one_or_none()
    if not config:
        config = BillingConfiguration(school_id=school_id)
        session.add(config)

    config.paystack_authorization_code = authorization.get("authorization_code")
    config.paystack_customer_code = (charge_data.get("customer") or {}).get("customer_code")
    config.paystack_card_last4 = authorization.get("last4")
    config.paystack_card_brand = authorization.get("card_type") or authorization.get("brand")
    config.updated_at = datetime.utcnow()
    session.add(config)
    logger.info(f"Saved reusable Paystack authorization for school {school_id} (auto-renewal)")


def get_payment_services():
    """Initialize payment services (lazy initialization)"""
    from services.online_payment_service import OnlinePaymentService
    from services.paystack_service import PaystackService
    
    paystack_secret_key = os.getenv("PAYSTACK_SECRET_KEY", "")
    
    if not paystack_secret_key:
        raise ValueError("PAYSTACK_SECRET_KEY not configured in .env")
    
    return {
        "paystack": PaystackService(paystack_secret_key),
        "online_payment": OnlinePaymentService(paystack_secret_key)
    }


# ============================================================================
# HELPER FUNCTIONS - PARENT-CHILD-FEE VALIDATION
# ============================================================================

async def get_parent_children_ids(current_user: User, session: AsyncSession) -> List[str]:
    """Get all student IDs for a parent's children"""
    parent_result = await session.execute(
        select(Parent).where(Parent.user_id == current_user.id)
    )
    parent = parent_result.scalar_one_or_none()
    
    if not parent:
        return []
    
    student_parent_result = await session.execute(
        select(StudentParent).where(StudentParent.parent_id == parent.id)
    )
    student_parents = student_parent_result.scalars().all()
    
    return [sp.student_id for sp in student_parents]


async def verify_parent_fee_access(
    fee_id: str, 
    current_user: User, 
    session: AsyncSession
) -> Fee:
    """
    Verify that a parent has access to a specific fee
    
    Validates:
    1. Fee exists
    2. Fee belongs to current user's school
    3. Fee's student is the parent's child
    
    Raises HTTPException if validation fails
    Returns: Fee object if validation passes
    """
    # Get fee
    fee_result = await session.execute(
        select(Fee).where(Fee.id == fee_id)
    )
    fee = fee_result.scalar_one_or_none()
    
    if not fee:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Fee not found"
        )
    
    # Validate school context
    if fee.school_id != current_user.school_id:
        logger.warning(
            f"Cross-school fee access attempt: parent_school={current_user.school_id}, "
            f"fee_school={fee.school_id}, parent_id={current_user.id}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access this fee"
        )
    
    # Validate parent-child relationship
    children_ids = await get_parent_children_ids(current_user, session)
    if fee.student_id not in children_ids:
        logger.warning(
            f"Unauthorized fee access attempt: fee belongs to student {fee.student_id}, "
            f"but parent {current_user.id} does not have access (children: {children_ids})"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to pay this fee"
        )
    
    return fee


async def check_duplicate_pending_transaction(
    fee_id: str,
    parent_id: str,
    amount: float,
    session: AsyncSession,
    tolerance: float = 0.01
) -> Optional[OnlineTransaction]:
    """
    Check if there's already a pending transaction for this fee
    
    Prevents duplicate payment initialization due to network retries
    
    Returns: Existing pending transaction if found, None otherwise
    """
    trans_result = await session.execute(
        select(OnlineTransaction).where(
            (OnlineTransaction.fee_id == fee_id) &
            (OnlineTransaction.parent_id == parent_id) &
            (OnlineTransaction.status == TransactionStatus.PENDING) &
            # Amount within tolerance (handles floating point issues)
            (OnlineTransaction.amount >= amount - tolerance) &
            (OnlineTransaction.amount <= amount + tolerance)
        ).order_by(OnlineTransaction.created_at.desc())
    )
    return trans_result.scalars().first()


# ============================================================================
# REQUEST/RESPONSE MODELS
# ============================================================================

class InitiatePaymentRequest(BaseModel):
    """Request to initiate payment"""
    fee_id: str
    amount_to_pay: Optional[float] = None  # Optional partial payment amount


class RequestMomoPaymentRequest(BaseModel):
    """School-initiated mobile-money payment prompt to a parent's phone"""
    fee_id: str
    provider: str  # "mtn" | "vod" (Telecel) | "atl" (AirtelTigo)
    phone: Optional[str] = None  # Defaults to the linked parent's registered phone
    amount_to_pay: Optional[float] = None  # Optional partial amount


class TransactionResponse:
    """Response for transaction status"""
    transaction_id: str
    fee_id: str
    student_id: str
    amount: float
    amount_paid: float
    status: str
    payment_url: Optional[str]
    reference: str
    created_at: str
    completed_at: Optional[str]


# ============================================================================
# ENDPOINTS
# ============================================================================

@router.post("/request-momo", status_code=200)
async def request_momo_payment(
    request_data: RequestMomoPaymentRequest,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("fees_plus")),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """Send a mobile-money payment prompt to a parent's phone (school-initiated).

    **Auth Required:** school_admin or super_admin

    The parent approves the charge on any phone — no smartphone, app, or
    browser needed. Confirmation lands through the standard Paystack webhook,
    so receipts and GL posting behave exactly like online payments.

    Providers: "mtn" (MTN MoMo), "vod" (Telecel Cash), "atl" (AT Money).
    """
    if current_user.role not in (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN):
        raise HTTPException(status_code=403, detail="Only school admins can request payments")
    if not current_user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    if request_data.provider not in ("mtn", "vod", "atl"):
        raise HTTPException(status_code=400, detail="Provider must be one of: mtn, vod, atl")

    try:
        services = get_payment_services()
    except ValueError:
        raise HTTPException(
            status_code=503,
            detail="Payment gateway not configured — add PAYSTACK_SECRET_KEY to enable mobile money prompts"
        )
    result = await services["online_payment"].request_momo_payment(
        session=session,
        fee_id=request_data.fee_id,
        school_id=current_user.school_id,
        provider=request_data.provider,
        phone=request_data.phone,
        amount_to_pay=request_data.amount_to_pay,
    )
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result.get("error", "Payment request failed"))
    return result


@router.post("/initialize", status_code=200)
async def initialize_payment(
    request_data: InitiatePaymentRequest,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("fees_plus")),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Initiate online payment for a fee
    
    **Auth Required:** Parent role
    
    **Request Body:**
    ```json
    {
        "fee_id": "uuid-of-fee",
        "amount_to_pay": 500.00  # Optional: for partial payments
    }
    ```
    
    **Returns:**
    ```json
    {
        "success": true,
        "transaction_id": "TXN-xxx",
        "payment_url": "https://checkout.paystack.com/...",
        "reference": "PAY-xxx",
        "amount": 500.00
    }
    ```
    
    **Errors:**
    - 400: Invalid request data
    - 403: Not authorized to pay this fee
    - 404: Fee not found
    - 500: Payment initialization failed
    
    **Security:**
    - Validates parent has access to fee's student
    - Validates fee belongs to parent's school
    - Validates amount doesn't exceed outstanding balance
    - Prevents duplicate payment initialization
    """
    
    try:
        # Verify user is parent
        if current_user.role != UserRole.PARENT:
            logger.warning(f"Non-parent user {current_user.id} attempted payment initialization")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only parents can initiate payments"
            )
        
        # Get fee and validate parent access (CRITICAL SECURITY CHECK)
        fee = await verify_parent_fee_access(
            request_data.fee_id,
            current_user,
            session
        )
        
        # Calculate amount to pay
        amount_due = fee.amount_due - fee.amount_paid - (fee.discount or 0)
        
        if amount_due <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No outstanding balance for this fee"
            )
        
        # Validate custom amount if provided
        if request_data.amount_to_pay is not None:
            if request_data.amount_to_pay <= 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Payment amount must be greater than zero"
                )
            if request_data.amount_to_pay > amount_due:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Payment amount cannot exceed outstanding balance of GHS {amount_due:.2f}"
                )
            payment_amount = request_data.amount_to_pay
        else:
            payment_amount = amount_due
        
        # Check for duplicate pending transaction (idempotency)
        existing_txn = await check_duplicate_pending_transaction(
            fee_id=request_data.fee_id,
            parent_id=current_user.id,
            amount=payment_amount,
            session=session
        )
        
        if existing_txn:
            logger.info(
                f"Duplicate payment initialization detected: "
                f"fee_id={request_data.fee_id}, parent_id={current_user.id}, "
                f"returning existing transaction {existing_txn.reference}"
            )
            return {
                "success": True,
                "transaction_id": existing_txn.reference,
                "payment_url": existing_txn.payment_url,
                "reference": existing_txn.reference,
                "amount": float(existing_txn.amount),
                "duplicate": True  # Flag to frontend that this is a retry
            }
        
        # Get services
        services = get_payment_services()
        online_payment_service = services["online_payment"]
        
        # Get parent's email from user context
        parent_email = current_user.email 
        # Get parent id from user context 
       
        
        # Initiate payment
        # NOTE: parent_id here is User.id, not Parent.id. Service layer converts it.
        result = await online_payment_service.initiate_payment(
            session=session,
            fee_id=request_data.fee_id,
            parent_id=current_user.id,  # User.id (service queries by Parent.user_id)
            parent_email=parent_email,
            school_id=current_user.school_id,
            amount_to_pay=payment_amount,
            # Pass fee object for additional validation in service
            fee=fee
        )
        
        if not result["success"]:
            logger.error(
                f"Payment initialization failed for fee {request_data.fee_id}: "
                f"{result.get('error', 'Unknown error')}"
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Payment initialization failed"
            )
        
        logger.info(
            f"Payment initiated successfully: txn_id={result['transaction_id']}, "
            f"fee_id={request_data.fee_id}, amount=GHS {payment_amount:.2f}, "
            f"parent_id={current_user.id}"
        )
        return result
    
    except HTTPException:
        raise
    except ValueError as e:
        logger.error(f"Configuration error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Payment system not configured"
        )
    except Exception as e:
        logger.error(f"Error initiating payment: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Payment initialization failed"
        )


async def _handle_payroll_transfer_webhook(session: AsyncSession, event: str, data: dict) -> None:
    """Reconcile a payroll disbursement's real outcome from Paystack's own
    async confirmation, instead of trusting only the synchronous
    initiate_transfer response — see services/payroll_service.py::
    disburse_payroll_run, which sets transfer_reference to
    "PAYROLL-{line_item_id}" before this webhook ever fires. Matched by
    that reference, not the line item's id directly, since Paystack echoes
    back whatever reference we originally sent."""
    from models.payroll import PayrollLineItem

    reference = data.get("reference")
    if not reference or not str(reference).startswith("PAYROLL-"):
        return

    line_item_id = str(reference)[len("PAYROLL-"):]
    result = await session.execute(select(PayrollLineItem).where(PayrollLineItem.id == line_item_id))
    line_item = result.scalar_one_or_none()
    if not line_item:
        logger.warning(f"Payroll transfer webhook for unknown line item (reference={reference})")
        return

    if event == "transfer.success":
        line_item.payment_status = "paid"
        line_item.payment_failure_reason = None
        if not line_item.paid_at:
            line_item.paid_at = datetime.utcnow()
    elif event in ("transfer.failed", "transfer.reversed"):
        # A transfer that looked accepted synchronously but actually
        # failed/reversed — flip it back to failed so disburse_payroll_run
        # picks it up again on retry (it only retries "unpaid"/"failed"
        # line items) instead of it staying stuck showing "paid" for money
        # that never actually arrived.
        line_item.payment_status = "failed"
        line_item.payment_failure_reason = f"Paystack {event}: {data.get('reason') or 'transfer did not complete'}"
        line_item.paid_at = None
    else:
        return

    line_item.updated_at = datetime.utcnow()
    session.add(line_item)
    await session.commit()
    logger.info(f"Payroll transfer webhook processed: {event} for line item {line_item_id}")


@router.post("/webhook/paystack", status_code=200)
async def paystack_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Webhook endpoint for Paystack payment notifications
    
    **No Auth Required** - Webhook is verified via HMAC signature
    
    **Paystack Calls This:**
    - When payment status changes
    - Includes HMAC-SHA512 signature in X-Paystack-Signature header
    
    **Returns:**
    ```json
    {
        "success": true,
        "processed": true
    }
    ```
    
    **Security:**
    - Verifies HMAC signature before processing
    - Rejects if signature doesn't match
    
    **Errors:**
    - 401: Invalid signature
    - 400: Missing signature or payload
    - 500: Webhook processing error
    """
    
    try:
        # Get services
        services = get_payment_services()
        paystack_service = services["paystack"]
        online_payment_service = services["online_payment"]
        
        # Import PaystackService for static method
        from services.paystack_service import PaystackService
        signature = request.headers.get("X-Paystack-Signature")
        
        if not signature:
            logger.warning("Webhook call without signature")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing X-Paystack-Signature header"
            )
        
        # Get raw body
        body = await request.body()
        
        # Get secret key
        paystack_secret_key = os.getenv("PAYSTACK_SECRET_KEY", "")
        
        # Verify signature
        is_valid = PaystackService.verify_webhook_signature(
            payload_bytes=body,
            signature=signature,
            secret_key=paystack_secret_key
        )
        
        if not is_valid:
            logger.warning(f"Invalid webhook signature")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid signature"
            )
        
        # Parse payload
        payload = json.loads(body)

        # Log for debugging
        event = payload.get("event")
        logger.info(f"Paystack webhook received: event={event}")

        # transfer.* events confirm/reject a payroll disbursement
        # asynchronously — disburse_payroll_run's synchronous
        # initiate_transfer response only means "Paystack accepted the
        # request," not "the money has actually arrived." Previously every
        # transfer.* event was silently dropped here, so a transfer that
        # went into "success" only via webhook (or later reversed) never
        # updated PayrollLineItem.payment_status at all.
        if event and str(event).startswith("transfer."):
            await _handle_payroll_transfer_webhook(session, event, payload.get("data", {}) or {})
            return {"success": True, "processed": True}

        # Only charge events represent money movement on our references.
        # Paystack also sends subscription.*, refund.* etc — a signed
        # non-charge, non-transfer event must never be treated as a payment.
        if event and not str(event).startswith("charge."):
            logger.info(f"Ignoring non-charge webhook event: {event}")
            return {"success": True, "processed": False}

        # Extract reference to determine payment type
        data = payload.get("data", {})
        reference = data.get("reference") if isinstance(data, dict) else payload.get("reference")
        
        if not reference:
            logger.warning("Webhook missing reference")
            return {"success": True, "processed": False}
        
        # Find transaction to determine if it's a platform subscription or individual fee payment
        trans_result = await session.execute(
            select(OnlineTransaction).where(OnlineTransaction.reference == reference)
        )
        transaction = trans_result.scalar_one_or_none()
        
        if not transaction:
            logger.warning(f"Transaction not found: {reference}")
            return {"success": True, "processed": False}
        
        # Route based on payment type - check reference prefix (more reliable than fee_id)
        if reference.startswith("PLAT-"):
            # Platform subscription payment - use billing service.
            # Only a successful charge may be applied to the subscription
            # ledger; charge.failed etc. must never credit anything.
            if event != "charge.success":
                logger.info(f"Ignoring non-success charge event for platform payment: {event} ({reference})")
                return {"success": True, "processed": False}

            logger.info(f"Platform subscription payment detected: {reference}")
            from services.platform_billing_service import PlatformBillingService

            paystack_secret_key = os.getenv("PAYSTACK_SECRET_KEY", "")
            if not paystack_secret_key:
                raise ValueError("PAYSTACK_SECRET_KEY not configured")

            billing_service = PlatformBillingService(paystack_secret_key)
            result = await billing_service.verify_and_process_payment(
                session=session,
                transaction_id=transaction.id,
                reference=reference,
                amount_paid=data.get("amount", 0) / 100 if isinstance(data, dict) else 0
            )
            logger.info(f"Billing webhook processed: {result}")

            if result.get("success") and isinstance(data, dict):
                await _save_paystack_authorization(session, transaction.school_id, data)
                await session.commit()

            return {"success": True, "processed": result.get("success", False)}
        else:
            # Individual fee payment - use online payment service
            logger.info(f"Individual fee payment detected: {reference}")
            online_payment_service = services["online_payment"]
            result = await online_payment_service.process_webhook(
                session=session,
                payload=payload,
                background_tasks=background_tasks
            )
            logger.info(f"Webhook processed: {payload.get('reference')}")
        return {"success": True, "processed": result.get("processed", True)}
    
    except ValueError as e:
        logger.error(f"Configuration error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Payment system not configured"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Webhook error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Webhook processing error"
        )


@router.get("/{transaction_id}", status_code=200)
async def get_transaction_status(
    transaction_id: str,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("fees_plus")),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Get payment transaction status
    
    **Auth Required:** Parent role
    
    **Path Parameters:**
    - transaction_id: UUID of the transaction
    
    **Returns:**
    ```json
    {
        "transaction_id": "TXN-xxx",
        "fee_id": "fee-xxx",
        "student_id": "student-xxx",
        "amount": 500.00,
        "amount_paid": 0.00,
        "status": "pending|processing|success|failed",
        "payment_url": "https://checkout.paystack.com/...",
        "reference": "PAY-xxx",
        "created_at": "2026-04-08T10:30:00",
        "completed_at": null
    }
    ```
    
    **Errors:**
    - 404: Transaction not found
    - 403: Not authorized to view this transaction
    """
    
    try:
        # Get transaction by ID (UUID)
        result = await session.execute(
            select(OnlineTransaction).where(
                and_(
                    OnlineTransaction.id == transaction_id,
                    OnlineTransaction.school_id == current_user.school_id
                )
            )
        )
        transaction = result.scalar_one_or_none()
        
        if not transaction:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Transaction not found"
            )
        
        # Verify authorization (parent can only see their own)
        # NOTE: transaction.parent_id stores Parent.id, not User.id
        # We need to look up the parent record to get the correct ID
        if current_user.role == UserRole.PARENT:
            parent_result = await session.execute(
                select(Parent).where(Parent.user_id == current_user.id)
            )
            parent = parent_result.scalar_one_or_none()
            
            if not parent or transaction.parent_id != parent.id:
                logger.warning(
                    f"Unauthorized transaction access attempt: "
                    f"user_id={current_user.id}, "
                    f"parent_id={parent.id if parent else 'None'}, "
                    f"transaction.parent_id={transaction.parent_id}"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Not authorized to view this transaction"
                )
        
        return {
            "transaction_id": transaction.reference,
            "fee_id": str(transaction.fee_id),
            "student_id": str(transaction.student_id),
            "amount": float(transaction.amount),
            "amount_paid": float(transaction.amount_paid or 0),
            "status": transaction.status.value,
            "payment_url": transaction.payment_url,
            "reference": transaction.reference,
            "created_at": transaction.created_at.isoformat() if transaction.created_at else None,
            "completed_at": transaction.completed_at.isoformat() if transaction.completed_at else None,
            "failed_reason": transaction.failed_reason
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting transaction: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error retrieving transaction"
        )


@router.get("/transactions/", status_code=200)
async def list_transactions(
    skip: int = 0,
    limit: int = 50,
    status_filter: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("fees_plus")),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    List payment transactions (filtered by role)
    
    **Auth Required:** Parent, Admin, or Accountant
    
    **Query Parameters:**
    - skip: Number of records to skip (default 0)
    - limit: Number of records to return (default 50, max 100)
    - status_filter: Filter by status (pending, processing, success, failed)
    
    **For Parents:** Only returns their own transactions
    **For Admin/Accountant:** Returns all transactions for school
    
    **Returns:**
    ```json
    {
        "total": 42,
        "count": 10,
        "skip": 0,
        "limit": 50,
        "transactions": [
            {
                "transaction_id": "TXN-xxx",
                "fee_id": "fee-xxx",
                "student_id": "student-xxx",
                "parent_id": "parent-xxx",
                "amount": 500.00,
                "status": "success",
                "reference": "PAY-xxx",
                "created_at": "2026-04-08T10:30:00"
            }
        ]
    }
    ```
    
    **Errors:**
    - 403: Insufficient permissions
    """
    
    try:
        # Validate limit
        limit = min(limit, 100)
        
        # Build query
        query = select(OnlineTransaction).where(
            OnlineTransaction.school_id == current_user.school_id
        )
        
        # Filter by role
        if current_user.role == UserRole.PARENT:
            # Get parent record to get the correct parent_id
            # NOTE: current_user.id is User.id, we need Parent.id for transaction filtering
            parent_result = await session.execute(
                select(Parent).where(Parent.user_id == current_user.id)
            )
            parent = parent_result.scalar_one_or_none()
            
            if parent:
                query = query.where(OnlineTransaction.parent_id == parent.id)
            else:
                # Parent not found, return empty list
                return {
                    "total": 0,
                    "count": 0,
                    "skip": skip,
                    "limit": limit,
                    "transactions": []
                }
        elif current_user.role not in [UserRole.SCHOOL_ADMIN, UserRole.ACCOUNTANT]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions"
            )
        
        # Filter by status if provided
        if status_filter:
            try:
                status_enum = TransactionStatus(status_filter)
                query = query.where(OnlineTransaction.status == status_enum)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid status: {status_filter}"
                )
        
        # Get total count
        count_result = await session.execute(select(OnlineTransaction).where(
            OnlineTransaction.school_id == current_user.school_id
        ))
        total = len(count_result.all())
        
        # Get paginated results
        result = await session.execute(query.offset(skip).limit(limit))
        transactions = result.scalars().all()
        
        return {
            "total": total,
            "count": len(transactions),
            "skip": skip,
            "limit": limit,
            "transactions": [
                {
                    "transaction_id": t.reference,
                    "fee_id": str(t.fee_id),
                    "student_id": str(t.student_id),
                    "parent_id": str(t.parent_id),
                    "amount": float(t.amount),
                    "status": t.status.value,
                    "reference": t.reference,
                    "created_at": t.created_at.isoformat() if t.created_at else None
                }
                for t in transactions
            ]
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing transactions: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error retrieving transactions"
        )


class MarkRefundedRequest(BaseModel):
    notes: Optional[str] = None
    amount: Optional[float] = None  # actual amount refunded, if it differs from the flagged amount


@router.get("/refunds/pending", status_code=200)
async def get_pending_fee_refunds(
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("fees_plus")),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    List school-fee transactions flagged for refund (a parent's payment
    arrived but the student had no more outstanding fees to apply it to).

    **Auth Required:** School Admin or Accountant
    """
    if current_user.role not in [UserRole.SCHOOL_ADMIN, UserRole.ACCOUNTANT]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")

    result = await session.execute(
        select(OnlineTransaction).where(
            OnlineTransaction.school_id == current_user.school_id,
            OnlineTransaction.transaction_type == TransactionType.FEE,
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
                "student_id": t.student_id,
                "parent_id": t.parent_id,
                "reference": t.reference,
                "amount_paid": t.amount_paid,
                "refund_amount": t.refund_amount,
                "reason": t.failed_reason,
                "completed_at": t.completed_at.isoformat() if t.completed_at else None,
            }
            for t in transactions
        ]
    }


@router.post("/{transaction_id}/mark-refunded", status_code=200)
async def mark_fee_refund_completed(
    transaction_id: str,
    body: MarkRefundedRequest = MarkRefundedRequest(),
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("fees_plus")),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Mark a flagged fee overpayment as refunded to the parent. Does not
    call a live payment-gateway refund API — closes out the manual
    refund queue entry once the parent has actually been paid back.

    **Auth Required:** School Admin or Accountant
    """
    if current_user.role not in [UserRole.SCHOOL_ADMIN, UserRole.ACCOUNTANT]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")

    txn_result = await session.execute(
        select(OnlineTransaction).where(
            OnlineTransaction.id == transaction_id,
            OnlineTransaction.school_id == current_user.school_id
        )
    )
    if not txn_result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found")

    from services.platform_billing_service import mark_transaction_refunded
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


@router.post("/{transaction_id}/verify", status_code=200)
async def verify_payment_with_paystack(
    transaction_id: str,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("fees_plus")),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Manually verify payment status with Paystack
    
    **Purpose**: Direct verification - bypass webhook if not configured
    
    **Supports**: Both individual fee payments and platform subscription payments
    
    **Auth Required:** Parent or Admin role
    
    **Path Parameters:**
    - transaction_id: Transaction UUID (database ID)
    
    **Returns:**
    ```json
    {
        "success": true,
        "status": "success|pending|failed",
        "message": "Payment verified",
        "updated": true/false,
        "amount": 500.00
    }
    ```
    """
    
    try:
        # Get transaction by ID (UUID), not by reference
        result = await session.execute(
            select(OnlineTransaction).where(
                and_(
                    OnlineTransaction.id == transaction_id,
                    OnlineTransaction.school_id == current_user.school_id
                )
            )
        )
        transaction = result.scalar_one_or_none()
        
        if not transaction:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Transaction not found"
            )
        
        # Verify authorization
        if current_user.role == UserRole.PARENT:
            # Parent can only verify their own fee payments
            parent_result = await session.execute(
                select(Parent).where(Parent.user_id == current_user.id)
            )
            parent = parent_result.scalar_one_or_none()
            
            if not parent or transaction.parent_id != parent.id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Not authorized"
                )
        elif current_user.role != UserRole.SCHOOL_ADMIN:
            # Only parent or school admin can verify
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions"
            )
        
        # If already success, return immediately
        if transaction.status == TransactionStatus.SUCCESS:
            return {
                "success": True,
                "status": "success",
                "message": "Payment already processed",
                "updated": False,
                "amount": float(transaction.amount)
            }
        
        # Verify with Paystack API
        services = get_payment_services()
        paystack_service = services["paystack"]
        
        logger.info(f"Verifying transaction {transaction_id} with Paystack")
        
        verify_result = await paystack_service.verify_payment(transaction.reference)
        
        if not verify_result["success"]:
            logger.warning(f"Paystack verification failed for {transaction_id}")
            return {
                "success": False,
                "status": "unknown",
                "message": "Could not verify with Paystack",
                "updated": False,
                "amount": float(transaction.amount)
            }
        
        paystack_data = verify_result["data"]
        paystack_status = paystack_data.get("status")
        
        logger.info(f"Paystack returned status: {paystack_status} for {transaction_id}")
        
        # If Paystack says successful, update transaction immediately
        if paystack_status == "success":
            logger.info(f"Paystack confirmed success - updating transaction {transaction_id}")
            
            amount_paid = paystack_data.get("amount", 0) / 100  # Convert from kobo
            
            # Route based on reference prefix - same as webhook
            if transaction.reference.startswith("PLAT-"):
                # Platform subscription payment
                logger.info(f"Verifying platform subscription payment: {transaction.reference}")
                from services.platform_billing_service import PlatformBillingService
                
                paystack_secret_key = os.getenv("PAYSTACK_SECRET_KEY", "")
                if not paystack_secret_key:
                    raise ValueError("PAYSTACK_SECRET_KEY not configured")
                
                billing_service = PlatformBillingService(paystack_secret_key)
                result = await billing_service.verify_and_process_payment(
                    session=session,
                    transaction_id=transaction.id,
                    reference=transaction.reference,
                    amount_paid=amount_paid
                )
                
                if result.get("success"):
                    logger.info(f"Platform subscription payment {transaction.reference} processed successfully")
                    return {
                        "success": True,
                        "status": "success",
                        "message": "Payment verified and processed",
                        "updated": True,
                        "amount": float(transaction.amount)
                    }
                else:
                    logger.error(f"Failed to process platform subscription: {result.get('error')}")
                    return {
                        "success": False,
                        "status": "error",
                        "message": f"Payment verified but processing failed: {result.get('error')}",
                        "updated": False,
                        "amount": float(transaction.amount)
                    }
            else:
                # Individual fee payment
                logger.info(f"Verifying individual fee payment: {transaction.reference}")
                online_payment_service = services["online_payment"]
                
                # Process webhook payload to trigger fee distribution
                webhook_payload = {
                    "event": "charge.success",
                    "data": paystack_data
                }
                
                result = await online_payment_service.process_webhook(
                    session=session,
                    payload=webhook_payload
                )
                
                if result.get("success"):
                    logger.info(f"Fee payment {transaction.reference} processed successfully with fee distribution")
                    return {
                        "success": True,
                        "status": "success",
                        "message": "Payment verified and processed",
                        "updated": True,
                        "amount": float(transaction.amount)
                    }
                else:
                    logger.error(f"Failed to process fee payment {transaction.reference}: {result.get('error')}")
                    return {
                        "success": False,
                        "status": "error",
                        "message": f"Payment verified but processing failed: {result.get('error')}",
                        "updated": False,
                        "amount": float(transaction.amount)
                    }
        else:
            # Payment not yet successful in Paystack
            logger.info(f"Payment not yet successful in Paystack - status: {paystack_status}")
            return {
                "success": True,
                "status": paystack_status or "pending",
                "message": f"Payment status: {paystack_status or 'pending'}",
                "updated": False,
                "amount": float(transaction.amount)
            }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Verification error: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )
