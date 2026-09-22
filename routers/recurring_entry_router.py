"""API Router for Recurring Journal Entry Templates

Endpoints for defining recurring entry templates (rent, insurance, monthly
accruals) and for the admin-triggered "run due" steps that turn them into
real journal entries — see RecurringEntryService for why this is
explicitly triggered rather than run on a background schedule.
"""
import logging
from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from models.finance.recurring_entry import (
    RecurringJournalEntryTemplateCreate,
    RecurringJournalEntryTemplateResponse,
    RecurringEntryLineTemplateResponse,
)
from models.user import User, UserRole
from dependencies import get_current_school_id
from auth import get_current_user, require_roles
from database import get_session
from services.recurring_entry_service import RecurringEntryService, RecurringEntryError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/recurring-entries", tags=["Finance - Recurring Entries"])

FINANCE_ADMIN_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR)


@router.post("/templates", response_model=RecurringJournalEntryTemplateResponse, status_code=201)
async def create_template(
    template_data: RecurringJournalEntryTemplateCreate,
    current_user: User = Depends(require_roles(*FINANCE_ADMIN_ROLES)),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
):
    """Create a recurring journal entry template

    **Access:** SUPER_ADMIN, SCHOOL_ADMIN, HR

    Line items must balance (debits = credits), same rule as a normal
    journal entry — generate-due feeds them straight into entry creation.
    """
    service = RecurringEntryService(session)
    try:
        template = await service.create_template(school_id, template_data, current_user.id)
        return RecurringJournalEntryTemplateResponse.model_validate(template)
    except RecurringEntryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating recurring template: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to create recurring template")


@router.get("/templates", response_model=List[RecurringJournalEntryTemplateResponse])
async def list_templates(
    active_only: bool = Query(True),
    current_user: User = Depends(get_current_user),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
):
    """List recurring entry templates

    **Access:** All authenticated users
    """
    service = RecurringEntryService(session)
    templates = await service.list_templates(school_id, active_only=active_only)

    responses = []
    for t in templates:
        lines = await service.get_template_lines(t.id)
        resp = RecurringJournalEntryTemplateResponse.model_validate(t)
        resp.line_items = [RecurringEntryLineTemplateResponse.model_validate(li) for li in lines]
        responses.append(resp)
    return responses


@router.post("/templates/{template_id}/deactivate", response_model=RecurringJournalEntryTemplateResponse)
async def deactivate_template(
    template_id: str,
    current_user: User = Depends(require_roles(*FINANCE_ADMIN_ROLES)),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
):
    """Stop a recurring entry template from generating further entries

    **Access:** SUPER_ADMIN, SCHOOL_ADMIN, HR
    """
    service = RecurringEntryService(session)
    try:
        template = await service.deactivate_template(school_id, template_id)
        return RecurringJournalEntryTemplateResponse.model_validate(template)
    except RecurringEntryError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/generate-due", response_model=dict)
async def generate_due_entries(
    as_of_date: Optional[str] = Query(None, description="ISO date; defaults to now"),
    current_user: User = Depends(require_roles(*FINANCE_ADMIN_ROLES)),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
):
    """Generate DRAFT journal entries for every recurring template due to run

    **Access:** SUPER_ADMIN, SCHOOL_ADMIN, HR

    Intended to be called periodically (e.g. by an external cron hitting
    this endpoint) — nothing runs this automatically. Safe to call more
    than once: a template only advances past a run once its entry is
    successfully created.
    """
    try:
        as_of = datetime.fromisoformat(as_of_date) if as_of_date else None
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid as_of_date format. Use ISO format.")

    service = RecurringEntryService(session)
    results = await service.generate_due_entries(school_id, created_by=current_user.id, as_of_date=as_of)
    return {
        "status": "success",
        "processed": len(results),
        "created": len([r for r in results if r["status"] == "created"]),
        "results": results,
    }


@router.post("/reverse-due-accruals", response_model=dict)
async def reverse_due_accruals(
    as_of_date: Optional[str] = Query(None, description="ISO date; defaults to now"),
    current_user: User = Depends(require_roles(*FINANCE_ADMIN_ROLES)),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
):
    """Reverse every posted accrual entry whose auto-reverse date has arrived

    **Access:** SUPER_ADMIN, SCHOOL_ADMIN, HR

    Same admin-triggered pattern as generate-due — call this periodically.
    """
    try:
        as_of = datetime.fromisoformat(as_of_date) if as_of_date else None
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid as_of_date format. Use ISO format.")

    service = RecurringEntryService(session)
    results = await service.reverse_due_accruals(school_id, reversed_by=current_user.id, as_of_date=as_of)
    return {
        "status": "success",
        "processed": len(results),
        "reversed": len([r for r in results if r["status"] == "reversed"]),
        "results": results,
    }
