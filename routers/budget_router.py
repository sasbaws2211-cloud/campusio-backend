"""API Router for Budgets

Endpoints for creating per-account budgets against a fiscal period, and
comparing budgeted amounts to actual posted activity.
"""
import logging
from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from models.finance.budget import (
    BudgetCreate, BudgetUpdate, BudgetResponse, BudgetVsActualLine,
    BudgetApprovalRequest, BudgetRejectionRequest,
    BudgetPlanCreate, BudgetPlanUpdate, BudgetPlanResponse,
)
from models.user import User
from dependencies import get_current_school_id
from auth import get_current_user, require_permission
from database import get_session
from services.budget_service import BudgetService, BudgetError
from services.plan_gating import require_plan_feature

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/budgets", tags=["Finance - Budgets"],
    dependencies=[Depends(require_plan_feature("finance_advanced"))],
)


@router.post("", response_model=BudgetResponse, status_code=201)
async def create_budget(
    budget_data: BudgetCreate,
    current_user: User = Depends(require_permission("finance.budget.manage")),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
):
    """Create a budget line for one GL account in one fiscal period

    **Access:** SUPER_ADMIN, SCHOOL_ADMIN, HR
    """
    service = BudgetService(session)
    try:
        budget = await service.create_budget_line(school_id, budget_data, current_user.id)
        return BudgetResponse.model_validate(budget)
    except BudgetError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating budget: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to create budget")


@router.get("", response_model=List[BudgetResponse])
async def list_budgets(
    fiscal_period_id: str,
    current_user: User = Depends(get_current_user),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
):
    """List all budget lines for a fiscal period

    **Access:** All authenticated users
    """
    service = BudgetService(session)
    budgets = await service.list_budgets(school_id, fiscal_period_id)
    return [BudgetResponse.model_validate(b) for b in budgets]


@router.put("/{budget_id}", response_model=BudgetResponse)
async def update_budget(
    budget_id: str,
    update_data: BudgetUpdate,
    current_user: User = Depends(require_permission("finance.budget.manage")),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
):
    """Update a budget line's amount or notes

    **Access:** SUPER_ADMIN, SCHOOL_ADMIN, HR
    """
    service = BudgetService(session)
    try:
        budget = await service.update_budget_line(school_id, budget_id, update_data)
        return BudgetResponse.model_validate(budget)
    except BudgetError as e:
        # "not found" -> 404; anything else (e.g. wrong status to edit) -> 400
        status_code = 404 if "not found" in str(e) else 400
        raise HTTPException(status_code=status_code, detail=str(e))
    except Exception as e:
        logger.error(f"Error updating budget: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to update budget")


@router.delete("/{budget_id}", status_code=204)
async def delete_budget(
    budget_id: str,
    current_user: User = Depends(require_permission("finance.budget.manage")),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
):
    """Delete a budget line

    **Access:** SUPER_ADMIN, SCHOOL_ADMIN, HR
    """
    service = BudgetService(session)
    try:
        await service.delete_budget_line(school_id, budget_id)
        return None
    except BudgetError as e:
        # "not found" -> 404; anything else (e.g. wrong status to delete) -> 400
        status_code = 404 if "not found" in str(e) else 400
        raise HTTPException(status_code=status_code, detail=str(e))


@router.post("/{budget_id}/submit", response_model=BudgetResponse)
async def submit_budget(
    budget_id: str,
    current_user: User = Depends(require_permission("finance.budget.manage")),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
):
    """Submit a draft (or previously rejected) budget line for approval

    **Access:** SUPER_ADMIN, SCHOOL_ADMIN, HR
    """
    service = BudgetService(session)
    try:
        budget = await service.submit_budget_line(school_id, budget_id, current_user.id)
        return BudgetResponse.model_validate(budget)
    except BudgetError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{budget_id}/approve", response_model=BudgetResponse)
async def approve_budget(
    budget_id: str,
    payload: BudgetApprovalRequest,
    current_user: User = Depends(require_permission("finance.budget.approve")),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
):
    """Approve a submitted budget line

    **Access:** SUPER_ADMIN, SCHOOL_ADMIN
    """
    service = BudgetService(session)
    try:
        budget = await service.approve_budget_line(school_id, budget_id, current_user.id, payload.approval_notes)
        return BudgetResponse.model_validate(budget)
    except BudgetError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{budget_id}/reject", response_model=BudgetResponse)
async def reject_budget(
    budget_id: str,
    payload: BudgetRejectionRequest,
    current_user: User = Depends(require_permission("finance.budget.approve")),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
):
    """Reject a submitted budget line

    **Access:** SUPER_ADMIN, SCHOOL_ADMIN
    """
    service = BudgetService(session)
    try:
        budget = await service.reject_budget_line(school_id, budget_id, current_user.id, payload.rejection_reason)
        return BudgetResponse.model_validate(budget)
    except BudgetError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/plans", response_model=BudgetPlanResponse, status_code=201)
async def create_budget_plan(
    plan_data: BudgetPlanCreate,
    current_user: User = Depends(require_permission("finance.budget.manage")),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
):
    """Create a multi-year budget plan that individual per-period budget
    lines can be linked to via BudgetCreate.budget_plan_id

    **Access:** SUPER_ADMIN, SCHOOL_ADMIN, HR
    """
    service = BudgetService(session)
    try:
        plan = await service.create_budget_plan(school_id, plan_data, current_user.id)
        return BudgetPlanResponse.model_validate(plan)
    except BudgetError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/plans", response_model=List[BudgetPlanResponse])
async def list_budget_plans(
    current_user: User = Depends(get_current_user),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
):
    """List all multi-year budget plans

    **Access:** All authenticated users
    """
    service = BudgetService(session)
    plans = await service.list_budget_plans(school_id)
    return [BudgetPlanResponse.model_validate(p) for p in plans]


@router.put("/plans/{plan_id}", response_model=BudgetPlanResponse)
async def update_budget_plan(
    plan_id: str,
    update_data: BudgetPlanUpdate,
    current_user: User = Depends(require_permission("finance.budget.manage")),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
):
    """Update a budget plan's name, status, or notes

    **Access:** SUPER_ADMIN, SCHOOL_ADMIN, HR
    """
    service = BudgetService(session)
    try:
        plan = await service.update_budget_plan(school_id, plan_id, update_data)
        return BudgetPlanResponse.model_validate(plan)
    except BudgetError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/plans/{plan_id}", status_code=204)
async def delete_budget_plan(
    plan_id: str,
    current_user: User = Depends(require_permission("finance.budget.manage")),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
):
    """Delete a budget plan (its linked budget lines are unaffected — only
    the plan grouping is removed)

    **Access:** SUPER_ADMIN, SCHOOL_ADMIN, HR
    """
    service = BudgetService(session)
    try:
        await service.delete_budget_plan(school_id, plan_id)
        return None
    except BudgetError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/plans/{plan_id}/vs-actual", response_model=List[BudgetVsActualLine])
async def get_budget_plan_vs_actual(
    plan_id: str,
    current_user: User = Depends(get_current_user),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
):
    """Compare budgeted amounts to actual posted activity across every
    fiscal period this multi-year plan's budget lines span

    **Access:** All authenticated users
    """
    service = BudgetService(session)
    try:
        return await service.get_budget_plan_vs_actual(school_id, plan_id)
    except BudgetError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Error generating budget-plan-vs-actual: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to generate budget-plan-vs-actual report")


@router.get("/vs-actual/{fiscal_period_id}", response_model=List[BudgetVsActualLine])
async def get_budget_vs_actual(
    fiscal_period_id: str,
    current_user: User = Depends(get_current_user),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
):
    """Compare budgeted amounts to actual posted activity for a fiscal period

    **Access:** All authenticated users

    Actual activity is bounded to the period's date range from posted
    journal entries — not accounts' running balances.
    """
    service = BudgetService(session)
    try:
        return await service.get_budget_vs_actual(school_id, fiscal_period_id)
    except BudgetError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Error generating budget-vs-actual: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to generate budget-vs-actual report")
