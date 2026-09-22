"""Settlement routes - handles MoMo withdrawals from Paystack"""
import logging
import os
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional

from database import get_session
from auth import get_current_user, require_roles
from models.user import User, UserRole
from services.settlement_service import SettlementService
from services.paystack_service import PaystackService
from services.plan_gating import require_plan_feature

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/settlements", tags=["settlements"],
    dependencies=[Depends(require_plan_feature("fees_plus"))],
)


def get_settlement_service():
    """Initialize settlement service (lazy initialization)"""
    paystack_secret_key = os.getenv("PAYSTACK_SECRET_KEY", "")
    
    if not paystack_secret_key:
        raise ValueError("PAYSTACK_SECRET_KEY not configured in .env")
    
    return SettlementService(paystack_secret_key)


# ============================================================================
# REQUEST/RESPONSE MODELS
# ============================================================================

class MoMoWithdrawalRequest(BaseModel):
    """Request to withdraw to MoMo"""
    momo_number: str  # e.g., "0244123456"
    recipient_name: str  # e.g., "School MoMo Account"
    amount: float  # Amount in GHS


class WithdrawalResponse(BaseModel):
    """Response for withdrawal"""
    success: bool
    transfer_code: Optional[str] = None
    amount: Optional[float] = None
    momo_number: Optional[str] = None
    status: Optional[str] = None
    error: Optional[str] = None


# ============================================================================
# ENDPOINTS
# ============================================================================

@router.get("/balance", status_code=200)
async def get_balance(
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session),
    settlement_service: SettlementService = Depends(get_settlement_service)
) -> dict:
    """
    Get school's settlement balance from transaction history
    
    **Auth Required:** School Admin or Finance role (school-scoped)
    
    **Returns:**
    ```json
    {
        "success": true,
        "balance": 50000.00,
        "currency": "GHS",
        "school_id": "school_123",
        "message": "Balance calculated from transaction history"
    }
    ```
    
    **Note:** Balance = (Fee Payments + Online Payments) - (Completed + Pending Withdrawals)
    """
    try:
        if not current_user.school_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No school context"
            )
        
        # Calculate balance from database (school-scoped)
        balance = await settlement_service.calculate_school_balance(
            session=session,
            school_id=current_user.school_id
        )
        
        logger.info(f"Balance fetched by {current_user.email} for school {current_user.school_id}: GHS {balance}")
        
        return {
            "success": True,
            "balance": balance,
            "currency": "GHS",
            "school_id": current_user.school_id,
            "message": "Balance calculated from transaction history"
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting balance: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get balance"
        )


@router.post("/withdraw-momo", status_code=200)
async def withdraw_to_momo(
    request_data: MoMoWithdrawalRequest,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session),
    settlement_service: SettlementService = Depends(get_settlement_service)
) -> dict:
    """
    Initiate MoMo withdrawal from Paystack
    
    **Auth Required:** Admin or Finance role
    
    **Request Body:**
    ```json
    {
        "momo_number": "0244123456",
        "recipient_name": "School MoMo Account",
        "amount": 5000.00
    }
    ```
    
    **Returns:**
    ```json
    {
        "success": true,
        "transfer_code": "TRF-xxx",
        "amount": 5000.00,
        "momo_number": "0244123456",
        "status": "pending",
        "message": "Withdrawal initiated. Transfer will complete in 2-4 hours"
    }
    ```
    
    **Errors:**
    - 400: Invalid request (invalid MoMo number, amount too low, etc.)
    - 403: Insufficient permissions
    - 500: Paystack API error
    """
    
    try:
        # Verify admin/finance role
        if current_user.role not in [UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admins and finance staff can initiate withdrawals"
            )
        
        # Initiate withdrawal
        result = await settlement_service.initiate_momo_withdrawal(
            session=session,
            momo_number=request_data.momo_number,
            recipient_name=request_data.recipient_name,
            amount=request_data.amount,
            school_id=current_user.school_id,
            initiated_by=current_user.id
        )
        
        if not result["success"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=result.get("error", "Withdrawal initiation failed")
            )
        
        logger.info(
            f"Withdrawal initiated by {current_user.email}: "
            f"GHS {request_data.amount} to {request_data.momo_number}"
        )
        
        return {
            **result,
            "message": "Withdrawal initiated. Transfer will complete in 2-4 hours"
        }
    
    except ValueError as e:
        logger.error(f"Configuration error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Payment system not configured"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error initiating MoMo withdrawal: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to initiate withdrawal"
        )


@router.get("/transactions/recent", status_code=200)
async def get_recent_transactions(
    days: int = 7,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session),
    settlement_service: SettlementService = Depends(get_settlement_service)
) -> dict:
    """
    Get recent online fee payments
    
    **Auth Required:** Admin or Finance role
    
    **Query Params:**
    - days: Number of days to look back (default: 7)
    
    **Returns:**
    ```json
    {
        "success": true,
        "count": 15,
        "transactions": [
            {
                "reference": "TXN-xxx",
                "amount": 500.00,
                "student_id": "...",
                "status": "success",
                "completed_at": "2026-04-13T10:30:00"
            }
        ]
    }
    ```
    """
    try:
        result = await settlement_service.get_recent_transactions(
            session=session,
            school_id=current_user.school_id,
            days=days,
            limit=50
        )
        return result
    
    except Exception as e:
        logger.error(f"Error getting recent transactions: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get transactions"
        )


@router.get("/withdrawals/history", status_code=200)
async def get_withdrawal_history(
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session),
    settlement_service: SettlementService = Depends(get_settlement_service)
) -> dict:
    """
    Get withdrawal history for this school
    
    **Auth Required:** Admin or Finance role
    
    **Returns:**
    ```json
    {
        "success": true,
        "count": 8,
        "withdrawals": [
            {
                "transfer_code": "TRF-xxx",
                "amount": 5000.00,
                "momo_number": "0244...",
                "status": "success",
                "initiated_at": "2026-04-10T14:20:00",
                "completed_at": "2026-04-10T16:45:00"
            }
        ]
    }
    ```
    """
    try:
        result = await settlement_service.get_withdrawal_history(
            session=session,
            school_id=current_user.school_id
        )
        return result
    
    except Exception as e:
        logger.error(f"Error getting withdrawal history: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get withdrawal history"
        )


@router.get("/transfer/{transfer_code}/status", status_code=200)
async def check_transfer_status(
    transfer_code: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    settlement_service: SettlementService = Depends(get_settlement_service)
) -> dict:
    """
    Check status of a specific transfer
    
    **Auth Required:** Admin or Finance role
    
    **Path Params:**
    - transfer_code: Paystack transfer code (e.g., "TRF-xxx")
    
    **Returns:**
    ```json
    {
        "success": true,
        "transfer_code": "TRF-xxx",
        "status": "success|pending|failed",
        "amount": 5000.00,
        "reason": "School fee settlement"
    }
    ```
    """
    try:
        result = await settlement_service.verify_transfer_status(transfer_code)
        return result
    
    except Exception as e:
        logger.error(f"Error checking transfer status: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to check transfer status"
        )


@router.post("/transfer/{transfer_code}/verify", status_code=200)
async def verify_and_update_transfer(
    transfer_code: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session),
    settlement_service: SettlementService = Depends(get_settlement_service)
) -> dict:
    """
    Verify transfer status with Paystack and update database
    
    **Auth Required:** Admin or Finance role
    
    **Purpose:** Direct verification - bypasses webhook dependency
    
    **Path Params:**
    - transfer_code: Paystack transfer code
    
    **Returns:**
    ```json
    {
        "success": true,
        "status": "success|pending|failed",
        "updated": true/false,
        "message": "Transfer verified and updated"
    }
    ```
    """
    try:
        logger.info(f"📋 [VERIFY] Starting verification for transfer: {transfer_code}")
        
        # Verify with Paystack
        paystack_service = PaystackService(os.getenv("PAYSTACK_SECRET_KEY", ""))
        logger.info(f"🔍 [VERIFY] Calling Paystack API for transfer: {transfer_code}")
        
        verify_result = await paystack_service.verify_transfer(transfer_code)
        
        logger.info(f"📡 [VERIFY] Paystack response: {verify_result}")
        
        if not verify_result.get("success", False):
            logger.warning(f"❌ [VERIFY] Paystack verification failed for {transfer_code}: {verify_result.get('error')}")
            return {
                "success": False,
                "status": "unknown",
                "updated": False,
                "message": f"Could not verify with Paystack: {verify_result.get('error', 'Unknown error')}"
            }
        
        paystack_status = verify_result.get("status")
        
        logger.info(f"✓ [VERIFY] Paystack status for {transfer_code}: {paystack_status}")
        
        # Update database if status changed
        if paystack_status == "success":
            logger.info(f"✅ [VERIFY] Paystack CONFIRMED COMPLETED for {transfer_code} - updating database")
            
            # Update transfer record in database
            result = await settlement_service.update_transfer_status(
                session=session,
                transfer_code=transfer_code,
                status="completed",
                paystack_data=verify_result
            )
            
            logger.info(f"💾 [VERIFY] Database update result: {result}")
            
            return {
                "success": True,
                "status": "success",
                "updated": result.get("updated", False),
                "message": "Transfer verified and updated to completed"
            }
        elif paystack_status == "failed":
            logger.info(f"⚠️ [VERIFY] Paystack marked as FAILED for {transfer_code} - updating database")
            
            result = await settlement_service.update_transfer_status(
                session=session,
                transfer_code=transfer_code,
                status="failed",
                paystack_data=verify_result
            )
            
            logger.info(f"💾 [VERIFY] Database update result: {result}")
            
            return {
                "success": True,
                "status": "failed",
                "updated": result.get("updated", False),
                "message": "Transfer verified and marked as failed"
            }
        else:
            # Still pending
            logger.info(f"⏳ [VERIFY] Transfer still PENDING for {transfer_code} - no database update")
            return {
                "success": True,
                "status": "pending",
                "updated": False,
                "message": f"Transfer status: {paystack_status or 'pending'}"
            }
    
    except Exception as e:
        logger.error(f"❌ [VERIFY] Verification error: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )
