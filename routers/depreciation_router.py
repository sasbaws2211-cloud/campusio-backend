"""API Router for Depreciation Schedules

Endpoints for defining straight-line depreciation schedules for fixed
assets, and the admin-triggered step that posts due monthly depreciation
entries.
"""
import logging
from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from models.finance.depreciation import (
    DepreciationScheduleCreate, DepreciationScheduleResponse,
    DisposeAssetRequest, DisposeAssetResponse,
)
from models.user import User
from dependencies import get_current_school_id
from auth import get_current_user, require_permission
from database import get_session
from services.depreciation_service import DepreciationService, DepreciationError
from services.plan_gating import require_plan_feature

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/depreciation", tags=["Finance - Depreciation"],
    dependencies=[Depends(require_plan_feature("finance_advanced"))],
)



@router.post("/schedules", response_model=DepreciationScheduleResponse, status_code=201)
async def create_schedule(
    schedule_data: DepreciationScheduleCreate,
    current_user: User = Depends(require_permission("finance.depreciation.manage")),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
):
    """Create a straight-line depreciation schedule for a fixed asset

    **Access:** SUPER_ADMIN, SCHOOL_ADMIN, HR

    Monthly depreciation = (asset_cost - salvage_value) / useful_life_months,
    computed automatically.
    """
    service = DepreciationService(session)
    try:
        schedule = await service.create_schedule(school_id, schedule_data, current_user.id)
        return DepreciationScheduleResponse.model_validate(schedule)
    except DepreciationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating depreciation schedule: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to create depreciation schedule")


@router.get("/schedules", response_model=List[DepreciationScheduleResponse])
async def list_schedules(
    active_only: bool = Query(True),
    current_user: User = Depends(get_current_user),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
):
    """List depreciation schedules

    **Access:** All authenticated users
    """
    service = DepreciationService(session)
    schedules = await service.list_schedules(school_id, active_only=active_only)
    return [DepreciationScheduleResponse.model_validate(s) for s in schedules]


@router.post("/schedules/{schedule_id}/deactivate", response_model=DepreciationScheduleResponse)
async def deactivate_schedule(
    schedule_id: str,
    current_user: User = Depends(require_permission("finance.depreciation.manage")),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
):
    """Stop a depreciation schedule from posting further entries

    **Access:** SUPER_ADMIN, SCHOOL_ADMIN, HR

    Use for a disposed/written-off asset before its useful life ends.
    """
    service = DepreciationService(session)
    try:
        schedule = await service.deactivate_schedule(school_id, schedule_id)
        return DepreciationScheduleResponse.model_validate(schedule)
    except DepreciationError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/schedules/{schedule_id}/dispose", response_model=DisposeAssetResponse)
async def dispose_asset(
    schedule_id: str,
    payload: DisposeAssetRequest,
    current_user: User = Depends(require_permission("finance.depreciation.manage")),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
):
    """Write off a fixed asset: remove its cost and accumulated
    depreciation from the books, recognize any gain/loss on disposal, and
    stop the schedule from posting further depreciation.

    **Access:** SUPER_ADMIN, SCHOOL_ADMIN, HR

    gain_loss_account_id should be an income/expense GL account for
    recording a gain or loss; cash_account_id is required only if
    proceeds > 0.
    """
    service = DepreciationService(session)
    try:
        result = await service.dispose_asset(
            school_id, schedule_id, payload.disposal_date, payload.proceeds,
            payload.gain_loss_account_id, payload.cash_account_id, current_user.id,
        )
        return DisposeAssetResponse(**result)
    except DepreciationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error disposing asset for schedule {schedule_id}: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to dispose asset")


@router.post("/generate-due", response_model=dict)
async def generate_due_depreciation(
    as_of_date: Optional[str] = Query(None, description="ISO date; defaults to now"),
    current_user: User = Depends(require_permission("finance.depreciation.manage")),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
):
    """Post the monthly depreciation entry for every schedule due to run

    **Access:** SUPER_ADMIN, SCHOOL_ADMIN, HR

    Intended to be called periodically (e.g. by an external cron hitting
    this endpoint, once a month) — nothing runs this automatically.
    """
    try:
        as_of = datetime.fromisoformat(as_of_date) if as_of_date else None
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid as_of_date format. Use ISO format.")

    service = DepreciationService(session)
    results = await service.generate_due_depreciation(school_id, created_by=current_user.id, as_of_date=as_of)
    return {
        "status": "success",
        "processed": len(results),
        "posted": len([r for r in results if r["status"] == "posted"]),
        "results": results,
    }
