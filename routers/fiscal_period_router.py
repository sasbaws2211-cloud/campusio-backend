"""API Router for Fiscal Period Management

Endpoints for:
- Creating and managing fiscal periods
- Period status transitions (lock, close)
- Period validation and querying
- Period-end procedures
"""
import logging
from typing import Optional, List
from sqlmodel import SQLModel
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime

from models.finance.fiscal_period import (
    FiscalPeriod,
    FiscalPeriodStatus,
    FiscalPeriodType,
    FiscalPeriodCreate,
    FiscalPeriodUpdate,
)
from services.fiscal_period_service import (
    FiscalPeriodService,
    FiscalPeriodError,
    FiscalPeriodValidationError,
)
from dependencies import get_current_school_id
from auth import get_current_user, require_roles
from database import get_session
from models.user import User, UserRole

# Fiscal period lifecycle (create/lock/close/set-current) is restricted to
# the same role set journal.py/coa.py already use for GL-affecting actions.
FINANCE_ADMIN_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/fiscal-periods", tags=["Fiscal Periods"])


class PeriodNotesRequest(SQLModel):
    notes: Optional[str] = None


class CheckPostingRequest(SQLModel):
    is_adjustment_entry: bool = False



# ==================== Create ====================

@router.post("/", response_model=dict)
async def create_fiscal_period(
    period_data: FiscalPeriodCreate,
    current_user: User = Depends(require_roles(*FINANCE_ADMIN_ROLES)),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
    request: Request = None,
) -> dict:
    """Create a new fiscal period
    
    Args:
        period_data: Period creation data
        current_user: Current authenticated user
        school_id: School identifier
        session: Database session
        request: HTTP request (for IP address)
        
    Returns:
        Created fiscal period data
        
    Raises:
        HTTPException 400: If validation fails
    """
    try:
        service = FiscalPeriodService(session)
        
        period = await service.create_period(
            school_id=school_id,
            period_data=period_data,
            created_by=current_user.id,
        )
        
        logger.info(
            f"Created fiscal period {period.period_name} for school {school_id} "
            f"by user {current_user.id}"
        )
        
        return {
            "id": period.id,
            "school_id": period.school_id,
            "period_name": period.period_name,
            "period_type": period.period_type.value,
            "start_date": period.start_date,
            "end_date": period.end_date,
            "fiscal_year": period.fiscal_year,
            "status": period.status.value,
            "is_current_period": period.is_current_period,
        }
    except FiscalPeriodValidationError as e:
        logger.warning(f"Fiscal period validation error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating fiscal period: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to create fiscal period")


# ==================== Read ====================

@router.get("/", response_model=list)
async def list_fiscal_periods(
    status: Optional[str] = Query(None),
    fiscal_year: Optional[int] = Query(None),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> list:
    """List fiscal periods with optional filtering
    
    Args:
        status: Filter by period status (optional)
        fiscal_year: Filter by fiscal year (optional)
        school_id: School identifier
        session: Database session
        
    Returns:
        List of fiscal periods
    """
    service = FiscalPeriodService(session)
    
    # Parse status enum if provided
    period_status = None
    if status:
        try:
            period_status = FiscalPeriodStatus(status)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status. Must be one of: {', '.join([s.value for s in FiscalPeriodStatus])}"
            )
    
    periods = await service.get_all_periods(
        school_id=school_id,
        status=period_status,
        fiscal_year=fiscal_year,
    )
    
    return [
        {
            "id": p.id,
            "period_name": p.period_name,
            "period_type": p.period_type.value,
            "start_date": p.start_date,
            "end_date": p.end_date,
            "fiscal_year": p.fiscal_year,
            "status": p.status.value,
            "is_current_period": p.is_current_period,
        }
        for p in periods
    ]


@router.get("/current", response_model=dict)
async def get_current_fiscal_period(
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Get the current open fiscal period
    
    Args:
        school_id: School identifier
        session: Database session
        
    Returns:
        Current fiscal period data
        
    Raises:
        HTTPException 404: If no open period exists
    """
    service = FiscalPeriodService(session)
    period = await service.get_current_period(school_id)
    
    if not period:
        raise HTTPException(status_code=404, detail="No open fiscal period found")
    
    return {
        "id": period.id,
        "period_name": period.period_name,
        "period_type": period.period_type.value,
        "start_date": period.start_date,
        "end_date": period.end_date,
        "fiscal_year": period.fiscal_year,
        "status": period.status.value,
        "allow_posting": period.allow_posting,
    }


@router.get("/by-date/{date_str}", response_model=dict)
async def get_period_by_date(
    date_str: str,
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Get fiscal period containing a specific date
    
    Args:
        date_str: Date in ISO format (YYYY-MM-DD)
        school_id: School identifier
        session: Database session
        
    Returns:
        Fiscal period data
        
    Raises:
        HTTPException 404: If no period contains the date
    """
    try:
        date = datetime.fromisoformat(date_str).date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
    
    service = FiscalPeriodService(session)
    period = await service.get_period_by_date(
        school_id, datetime.combine(date, datetime.min.time())
    )
    
    if not period:
        raise HTTPException(status_code=404, detail="No fiscal period found for the given date")
    
    return {
        "id": period.id,
        "period_name": period.period_name,
        "start_date": period.start_date,
        "end_date": period.end_date,
        "status": period.status.value,
    }


@router.get("/summary", response_model=dict)
async def get_period_summary(
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Get summary of all fiscal periods
    
    Args:
        school_id: School identifier
        session: Database session
        
    Returns:
        Period summary with counts by status and fiscal year
    """
    service = FiscalPeriodService(session)
    return await service.get_period_summary(school_id)


@router.get("/{period_id}", response_model=dict)
async def get_fiscal_period(
    period_id: str,
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Get a fiscal period by ID
    
    Args:
        period_id: Period ID
        school_id: School identifier
        session: Database session
        
    Returns:
        Fiscal period data
        
    Raises:
        HTTPException 404: If period not found
    """
    service = FiscalPeriodService(session)
    period = await service.get_period_by_id(school_id, period_id)
    
    if not period:
        raise HTTPException(status_code=404, detail="Fiscal period not found")
    
    return {
        "id": period.id,
        "period_name": period.period_name,
        "period_type": period.period_type.value,
        "start_date": period.start_date,
        "end_date": period.end_date,
        "fiscal_year": period.fiscal_year,
        "status": period.status.value,
        "allow_posting": period.allow_posting,
        "allow_adjustment_entries": period.allow_adjustment_entries,
        "is_current_period": period.is_current_period,
        "locked_date": period.locked_date,
        "closed_date": period.closed_date,
        "notes": period.notes,
    }


# ==================== Status Transitions ====================

@router.post("/{period_id}/lock", response_model=dict)
async def lock_fiscal_period(
    period_id: str,
    body: PeriodNotesRequest = PeriodNotesRequest(),
    current_user: User = Depends(require_roles(*FINANCE_ADMIN_ROLES)),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    notes = body.notes
    """Lock a fiscal period (end of audit, before closing)
    
    Args:
        period_id: Period ID to lock
        notes: Optional notes about the lock
        current_user: Current user
        school_id: School identifier
        session: Database session
        
    Returns:
        Updated fiscal period data
        
    Raises:
        HTTPException 400: If period cannot be locked
        HTTPException 404: If period not found
    """
    try:
        service = FiscalPeriodService(session)
        
        period = await service.lock_period(
            school_id=school_id,
            period_id=period_id,
            locked_by=current_user.id,
            notes=notes,
        )
        
        if not period:
            raise HTTPException(status_code=404, detail="Fiscal period not found")
        
        logger.info(f"Locked fiscal period {period_id} by user {current_user.id}")
        
        return {
            "id": period.id,
            "period_name": period.period_name,
            "status": period.status.value,
            "allow_posting": period.allow_posting,
            "locked_date": period.locked_date,
        }
    except FiscalPeriodError as e:
        logger.warning(f"Fiscal period error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error locking fiscal period: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to lock fiscal period")


@router.post("/{period_id}/close", response_model=dict)
async def close_fiscal_period(
    period_id: str,
    body: PeriodNotesRequest = PeriodNotesRequest(),
    current_user: User = Depends(require_roles(*FINANCE_ADMIN_ROLES)),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    notes = body.notes
    """Close a fiscal period (after closing entries are posted)
    
    Args:
        period_id: Period ID to close
        notes: Optional notes about the close
        current_user: Current user
        school_id: School identifier
        session: Database session
        
    Returns:
        Updated fiscal period data
        
    Raises:
        HTTPException 400: If period cannot be closed
        HTTPException 404: If period not found
    """
    try:
        service = FiscalPeriodService(session)
        
        period = await service.close_period(
            school_id=school_id,
            period_id=period_id,
            closed_by=current_user.id,
            notes=notes,
        )
        
        if not period:
            raise HTTPException(status_code=404, detail="Fiscal period not found")
        
        logger.info(f"Closed fiscal period {period_id} by user {current_user.id}")
        
        return {
            "id": period.id,
            "period_name": period.period_name,
            "status": period.status.value,
            "allow_posting": period.allow_posting,
            "closed_date": period.closed_date,
        }
    except FiscalPeriodError as e:
        logger.warning(f"Fiscal period error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error closing fiscal period: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to close fiscal period")


@router.post("/{period_id}/set-current", response_model=dict)
async def set_current_period(
    period_id: str,
    current_user: User = Depends(require_roles(*FINANCE_ADMIN_ROLES)),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Set a period as the current posting period
    
    Args:
        period_id: Period ID to set as current
        current_user: Current user
        school_id: School identifier
        session: Database session
        
    Returns:
        Updated fiscal period data
        
    Raises:
        HTTPException 404: If period not found
    """
    service = FiscalPeriodService(session)
    period = await service.set_current_period(school_id, period_id)
    
    if not period:
        raise HTTPException(status_code=404, detail="Fiscal period not found")
    
    logger.info(f"Set {period.period_name} as current period by user {current_user.id}")
    
    return {
        "id": period.id,
        "period_name": period.period_name,
        "is_current_period": period.is_current_period,
    }


# ==================== Analysis ====================

@router.post("/{period_id}/check-posting", response_model=dict)
async def check_if_can_post_to_period(
    period_id: str,
    body: CheckPostingRequest = CheckPostingRequest(),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    is_adjustment_entry = body.is_adjustment_entry
    """Check if transactions can be posted to a period
    
    Args:
        period_id: Period ID to check
        is_adjustment_entry: Whether this is an adjusting entry
        school_id: School identifier
        session: Database session
        
    Returns:
        Dict with can_post (bool) and reason (str)
    """
    service = FiscalPeriodService(session)
    can_post, reason = await service.can_post_to_period(
        school_id, period_id, is_adjustment_entry
    )
    
    return {
        "can_post": can_post,
        "reason": reason,
    }
