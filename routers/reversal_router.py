"""API Router for Reversal Management

Endpoints for:
- Full entry reversals
- Partial reversals (specific line items or accounts)
- Reversal analysis and reporting
- Reversal chain tracking
"""
import logging
from typing import Optional, List
from sqlmodel import SQLModel
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime

from services.reversal_service import ReversalService, ReversalError
from dependencies import get_current_school_id
from auth import get_current_user, require_roles
from database import get_session
from models.user import User, UserRole

# Reversing a posted entry is a GL-affecting write — same role gate as
# journal.py's own reverse endpoint.
FINANCE_ADMIN_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/reversals", tags=["Reversals"])


# ── Request bodies ──

class FullReversalRequest(SQLModel):
    reversal_reason: str
    reversal_notes: Optional[str] = None


class PartialReversalRequest(SQLModel):
    line_numbers: List[int]
    reversal_reason: str
    reversal_notes: Optional[str] = None


class AccountReversalRequest(SQLModel):
    account_ids: List[str]
    reversal_reason: str
    reversal_notes: Optional[str] = None



# ==================== Full Reversal ====================

@router.post("/{entry_id}/reverse-full", response_model=dict)
async def reverse_full_entry(
    entry_id: str,
    body: FullReversalRequest,
    current_user: User = Depends(require_roles(*FINANCE_ADMIN_ROLES)),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
    request: Request = None,
) -> dict:
    reversal_reason = body.reversal_reason
    reversal_notes = body.reversal_notes
    """Reverse an entire posted journal entry
    
    Creates a complete contra-entry that reverses all line items.
    Original and reversal both visible in audit trail.
    
    Args:
        entry_id: Entry ID to reverse
        reversal_reason: Reason for reversal (required)
        reversal_notes: Optional additional notes
        current_user: Current authenticated user
        school_id: School identifier
        session: Database session
        request: HTTP request (for IP address)
        
    Returns:
        Dictionary with original and reversal entry data
        
    Raises:
        HTTPException 400: If entry cannot be reversed
        HTTPException 404: If entry not found
    """
    try:
        service = ReversalService(session)
        
        ip_address = request.client.host if request else None
        user_role = current_user.role.value if current_user.role else "finance"
        user_name = f"{current_user.first_name} {current_user.last_name}"

        original_entry, reversal_entry = await service.reverse_full_entry(
            school_id=school_id,
            entry_id=entry_id,
            reversed_by=current_user.id,
            reversal_reason=reversal_reason,
            reversal_notes=reversal_notes,
            ip_address=ip_address,
            user_role=user_role,
            user_name=user_name,
        )

        logger.info(
            f"Full reversal of entry {entry_id} completed by {current_user.id} "
            f"(reason: {reversal_reason})"
        )
        
        return {
            "status": "success",
            "original_entry": {
                "id": original_entry.id,
                "status": original_entry.posting_status.value,
                "amount": float(original_entry.total_debit),
            },
            "reversal_entry": {
                "id": reversal_entry.id,
                "status": reversal_entry.posting_status.value,
                "amount": float(reversal_entry.total_debit),
                "posted_date": reversal_entry.posted_date.isoformat() if reversal_entry.posted_date else None,
            },
            "reversal_reason": reversal_reason,
        }
    except ReversalError as e:
        logger.warning(f"Reversal error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error reversing entry: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to reverse entry")


# ==================== Partial Reversal ====================

@router.post("/{entry_id}/reverse-partial", response_model=dict)
async def reverse_partial_entry(
    entry_id: str,
    body: PartialReversalRequest,
    current_user: User = Depends(require_roles(*FINANCE_ADMIN_ROLES)),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
    request: Request = None,
) -> dict:
    line_numbers = body.line_numbers
    reversal_reason = body.reversal_reason
    reversal_notes = body.reversal_notes
    """Reverse specific line items from a posted entry
    
    Creates a contra-entry for only selected line items.
    
    Args:
        entry_id: Entry ID to partially reverse
        line_numbers: List of line numbers to reverse
        reversal_reason: Reason for reversal (required)
        reversal_notes: Optional notes
        current_user: Current user
        school_id: School identifier
        session: Database session
        request: HTTP request
        
    Returns:
        Dictionary with reversal results
        
    Raises:
        HTTPException 400: If lines don't balance or other error
        HTTPException 404: If entry not found
    """
    try:
        if not line_numbers:
            raise HTTPException(status_code=400, detail="Must specify at least one line number")
        
        service = ReversalService(session)
        
        ip_address = request.client.host if request else None
        user_role = current_user.role.value if current_user.role else "finance"
        user_name = f"{current_user.first_name} {current_user.last_name}"

        original_entry, reversal_entry = await service.reverse_partial_entry(
            school_id=school_id,
            entry_id=entry_id,
            line_numbers=line_numbers,
            reversed_by=current_user.id,
            reversal_reason=reversal_reason,
            reversal_notes=reversal_notes,
            ip_address=ip_address,
            user_role=user_role,
            user_name=user_name,
        )

        logger.info(
            f"Partial reversal of entry {entry_id} (lines: {line_numbers}) "
            f"completed by {current_user.id}"
        )
        
        return {
            "status": "success",
            "original_entry_id": original_entry.id,
            "reversal_entry_id": reversal_entry.id,
            "lines_reversed": line_numbers,
            "reversal_amount": float(reversal_entry.total_debit),
        }
    except ReversalError as e:
        logger.warning(f"Partial reversal error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error in partial reversal: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to reverse entry partially")


# ==================== Account-Specific Reversal ====================

@router.post("/{entry_id}/reverse-accounts", response_model=dict)
async def reverse_specific_accounts(
    entry_id: str,
    body: AccountReversalRequest,
    current_user: User = Depends(require_roles(*FINANCE_ADMIN_ROLES)),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
    request: Request = None,
) -> dict:
    account_ids = body.account_ids
    reversal_reason = body.reversal_reason
    reversal_notes = body.reversal_notes
    """Reverse postings to specific GL accounts only
    
    Creates a contra-entry for selected accounts.
    
    Args:
        entry_id: Entry ID to reverse
        account_ids: List of GL account IDs to reverse
        reversal_reason: Reason for reversal
        reversal_notes: Optional notes
        current_user: Current user
        school_id: School identifier
        session: Database session
        request: HTTP request
        
    Returns:
        Dictionary with reversal results
        
    Raises:
        HTTPException 400: If accounts not found or don't balance
    """
    try:
        if not account_ids:
            raise HTTPException(status_code=400, detail="Must specify at least one account")
        
        service = ReversalService(session)
        
        ip_address = request.client.host if request else None
        user_role = current_user.role.value if current_user.role else "finance"
        user_name = f"{current_user.first_name} {current_user.last_name}"

        original_entry, reversal_entry = await service.reverse_specific_accounts(
            school_id=school_id,
            entry_id=entry_id,
            account_ids=account_ids,
            reversed_by=current_user.id,
            reversal_reason=reversal_reason,
            reversal_notes=reversal_notes,
            ip_address=ip_address,
            user_role=user_role,
            user_name=user_name,
        )

        logger.info(
            f"Account-specific reversal of entry {entry_id} (accounts: {account_ids}) "
            f"by {current_user.id}"
        )
        
        return {
            "status": "success",
            "original_entry_id": original_entry.id,
            "reversal_entry_id": reversal_entry.id,
            "accounts_reversed": account_ids,
            "reversal_amount": float(reversal_entry.total_debit),
        }
    except ReversalError as e:
        logger.warning(f"Account reversal error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error in account reversal: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to reverse accounts")


# ==================== Reversal Analysis ====================

@router.get("/{entry_id}/chain", response_model=dict)
async def get_reversal_chain(
    entry_id: str,
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Get complete reversal chain for an entry
    
    Shows: Original → Reversal → Re-reversal (if applicable)
    
    Args:
        entry_id: Entry ID to trace
        school_id: School identifier
        session: Database session
        
    Returns:
        Complete reversal chain
    """
    service = ReversalService(session)
    return await service.get_reversal_chain(school_id, entry_id)


@router.get("/period-reversals", response_model=list)
async def get_reversals_for_period(
    start_date: str = Query(...),
    end_date: str = Query(...),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> list:
    """Get all reversals within a date range
    
    Useful for audit and analysis.
    
    Args:
        start_date: Start date in ISO format
        end_date: End date in ISO format
        school_id: School identifier
        session: Database session
        
    Returns:
        List of reversal entries
        
    Raises:
        HTTPException 400: If dates are invalid
    """
    try:
        start_dt = datetime.fromisoformat(start_date)
        end_dt = datetime.fromisoformat(end_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use ISO format.")
    
    service = ReversalService(session)
    reversals = await service.get_reversals_for_period(school_id, start_dt, end_dt)
    
    return reversals


@router.get("/statistics", response_model=dict)
async def get_reversal_statistics(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Get statistics on reversals
    
    Args:
        start_date: Optional start date filter
        end_date: Optional end date filter
        school_id: School identifier
        session: Database session
        
    Returns:
        Reversal statistics
    """
    start_dt = None
    end_dt = None
    
    if start_date:
        try:
            start_dt = datetime.fromisoformat(start_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid start_date format")
    
    if end_date:
        try:
            end_dt = datetime.fromisoformat(end_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid end_date format")
    
    service = ReversalService(session)
    return await service.get_reversal_statistics(school_id, start_dt, end_dt)
