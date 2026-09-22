"""API Router for Retained Earnings & Period Close

Endpoints for:
- Net income calculation
- Period closing (creates closing entries)
- Opening balance setup for next period
- Post-closing trial balance
- Period close verification
"""
import logging
from typing import Optional
from sqlmodel import SQLModel
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime

from services.retained_earnings_service import RetainedEarningsService, RetainedEarningsError
from dependencies import get_current_school_id
from auth import get_current_user, require_roles
from database import get_session
from models.user import User, UserRole

# Period close and opening-balance rollover are irreversible GL operations —
# same role gate as journal.py's posting/reversal endpoints.
FINANCE_ADMIN_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/retained-earnings", tags=["Retained Earnings"])


class SetOpeningBalancesRequest(SQLModel):
    from_period_id: str
    to_period_id: str



# ==================== Net Income ====================

@router.get("/net-income/{period_id}", response_model=dict)
async def get_net_income(
    period_id: str,
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Calculate net income for a fiscal period
    
    Net Income = Total Revenue - Total Expenses
    
    Args:
        period_id: Fiscal period ID
        school_id: School identifier
        session: Database session
        
    Returns:
        Net income calculation with revenue, expenses breakdown
    """
    try:
        service = RetainedEarningsService(session)
        result = await service.calculate_net_income(school_id, period_id)
        return result
    except RetainedEarningsError as e:
        logger.warning(f"Error calculating net income: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error in net income calculation: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to calculate net income")


# ==================== Period Close ====================

@router.post("/close-period/{period_id}", response_model=dict)
async def close_fiscal_period(
    period_id: str,
    current_user: User = Depends(require_roles(*FINANCE_ADMIN_ROLES)),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
    request: Request = None,
) -> dict:
    """Close a fiscal period
    
    **CRITICAL OPERATION** - Creates closing entries to:
    - Zero out all revenue and expense accounts
    - Post net income to retained earnings
    - Transition period to CLOSED status
    
    Args:
        period_id: Period ID to close (must be LOCKED)
        current_user: Current user
        school_id: School identifier
        session: Database session
        request: HTTP request
        
    Returns:
        Closing results with entry ID and statistics
        
    Raises:
        HTTPException 400: If period cannot be closed
        HTTPException 404: If period not found
    """
    try:
        service = RetainedEarningsService(session)
        
        ip_address = request.client.host if request else None
        user_role = current_user.role.value if current_user.role else "finance"
        user_name = f"{current_user.first_name} {current_user.last_name}"

        result = await service.close_period(
            school_id=school_id,
            period_id=period_id,
            closed_by=current_user.id,
            ip_address=ip_address,
            user_role=user_role,
            user_name=user_name,
        )

        logger.info(
            f"Period {period_id} closed by {current_user.id} "
            f"(net income: {result['net_income']:.2f})"
        )
        
        return result
    except RetainedEarningsError as e:
        logger.warning(f"Period close error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error closing period: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to close period")


# ==================== Opening Balances ====================

@router.post("/set-opening-balances", response_model=dict)
async def set_opening_balances(
    body: SetOpeningBalancesRequest,
    current_user: User = Depends(require_roles(*FINANCE_ADMIN_ROLES)),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from_period_id = body.from_period_id
    to_period_id = body.to_period_id
    """Set opening balances for next period from previous period close
    
    Balance sheet accounts (Asset, Liability, Equity) carry forward their
    closing balance as opening balance for next period.
    
    Args:
        from_period_id: Previous closed period ID
        to_period_id: New period to set opening balances for
        current_user: Current user
        school_id: School identifier
        session: Database session
        
    Returns:
        Summary of opening balances set
        
    Raises:
        HTTPException 400: If periods not found or cannot set balances
    """
    try:
        service = RetainedEarningsService(session)
        
        result = await service.set_opening_balances_for_period(
            school_id=school_id,
            from_period_id=from_period_id,
            to_period_id=to_period_id,
            created_by=current_user.id,
            user_role=current_user.role.value if current_user.role else "finance",
            user_name=f"{current_user.first_name} {current_user.last_name}",
        )

        logger.info(
            f"Opening balances set for period {to_period_id} "
            f"({result['accounts_updated']} accounts) by {current_user.id}"
        )
        
        return result
    except RetainedEarningsError as e:
        logger.warning(f"Error setting opening balances: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error in opening balance setup: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to set opening balances")


# ==================== Trial Balance & Verification ====================

@router.get("/post-closing-trial-balance/{period_id}", response_model=dict)
async def get_post_closing_trial_balance(
    period_id: str,
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Get post-closing trial balance
    
    After period close, only balance sheet accounts should have balances.
    Revenue and expense accounts should all be zero.
    
    Args:
        period_id: Period ID
        school_id: School identifier
        session: Database session
        
    Returns:
        Trial balance with accounts by type
    """
    try:
        service = RetainedEarningsService(session)
        return await service.get_post_closing_trial_balance(school_id, period_id)
    except Exception as e:
        logger.error(f"Error getting post-closing trial balance: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get trial balance")


@router.get("/verify-period-close/{period_id}", response_model=dict)
async def verify_period_closed(
    period_id: str,
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Verify a period is properly closed
    
    Checks:
    - Period status is CLOSED
    - All revenue/expense accounts are zero
    - Trial balance is balanced
    
    Args:
        period_id: Period ID to verify
        school_id: School identifier
        session: Database session
        
    Returns:
        Verification results with any issues found
    """
    service = RetainedEarningsService(session)
    return await service.verify_period_closed(school_id, period_id)


# ==================== Retained Earnings Balance ====================

@router.get("/balance", response_model=dict)
async def get_retained_earnings_balance(
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Get current retained earnings balance
    
    Args:
        school_id: School identifier
        session: Database session
        
    Returns:
        Current retained earnings balance
    """
    try:
        service = RetainedEarningsService(session)
        balance = await service.get_retained_earnings_balance(school_id)
        account = await service.coa_service.get_system_account(
            school_id=school_id, system_role="retained_earnings", fallback_code="3100",
        )

        return {
            "school_id": school_id,
            "account_code": account.account_code if account else "3100",
            "account_name": account.account_name if account else "Retained Earnings",
            "balance": balance,
            "as_of": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        logger.error(f"Error getting retained earnings balance: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get retained earnings balance")
