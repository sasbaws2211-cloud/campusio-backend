"""Admin settings + manual trigger for parent fee-due reminders — the
scheduled sweep itself lives in services/fee_reminder_service.py and
services/scheduler.py."""
from __future__ import annotations

from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_roles
from database import get_session
from models.fee_reminders import FeeReminderSettings, FeeReminderSettingsUpdate, FeeReminder
from models.user import User, UserRole
from services.fee_reminder_service import get_or_create_settings, send_pending_reminders
from services.fee_late_fee_service import apply_late_fees
from services.plan_gating import require_plan_feature

router = APIRouter(
    prefix="/fee-reminders", tags=["Fee Reminders"],
    dependencies=[Depends(require_plan_feature("fees_plus"))],
)

WRITE_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)


def _school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


def _settings_dict(settings: FeeReminderSettings) -> dict:
    return {
        "enabled": settings.enabled,
        "reminder_days_before_due": settings.reminder_days_before_due,
        "enable_late_fees": settings.enable_late_fees,
        "late_fee_percentage": settings.late_fee_percentage,
        "late_fee_grace_days": settings.late_fee_grace_days,
    }


@router.get("/settings", response_model=dict)
async def get_settings(current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    settings = await get_or_create_settings(session, _school_id(current_user))
    await session.commit()
    return _settings_dict(settings)


@router.put("/settings", response_model=dict)
async def update_settings(payload: FeeReminderSettingsUpdate, current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    settings = await get_or_create_settings(session, _school_id(current_user))
    if payload.enabled is not None:
        settings.enabled = payload.enabled
    if payload.reminder_days_before_due is not None:
        if payload.reminder_days_before_due < 0:
            raise HTTPException(status_code=422, detail="reminder_days_before_due cannot be negative")
        settings.reminder_days_before_due = payload.reminder_days_before_due
    if payload.enable_late_fees is not None:
        settings.enable_late_fees = payload.enable_late_fees
    if payload.late_fee_percentage is not None:
        if payload.late_fee_percentage < 0:
            raise HTTPException(status_code=422, detail="late_fee_percentage cannot be negative")
        settings.late_fee_percentage = payload.late_fee_percentage
    if payload.late_fee_grace_days is not None:
        if payload.late_fee_grace_days < 0:
            raise HTTPException(status_code=422, detail="late_fee_grace_days cannot be negative")
        settings.late_fee_grace_days = payload.late_fee_grace_days
    settings.updated_at = datetime.utcnow()
    await session.commit()
    return _settings_dict(settings)


@router.post("/run-now", response_model=dict)
async def run_now(current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    """Manual trigger for this school's reminder sweep, same logic the
    nightly job runs across every school — lets an admin send today's
    reminders on demand instead of waiting for the scheduled run."""
    return await send_pending_reminders(session, school_id=_school_id(current_user))


@router.post("/apply-late-fees", response_model=dict)
async def apply_late_fees_now(current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    """Manual trigger for this school's late-fee sweep, same logic the
    nightly job runs across every school. Only charges fees that haven't
    already had a penalty applied (see Fee.late_fee_amount)."""
    return await apply_late_fees(session, school_id=_school_id(current_user))


@router.get("/history", response_model=List[dict])
async def reminder_history(
    student_id: str | None = Query(default=None),
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    stmt = select(FeeReminder).where(FeeReminder.school_id == school_id)
    if student_id:
        stmt = stmt.where(FeeReminder.student_id == student_id)
    result = await session.execute(stmt.order_by(FeeReminder.created_at.desc()).limit(100))
    return [
        {
            "id": r.id, "fee_id": r.fee_id, "student_id": r.student_id, "channel": r.channel,
            "days_until_due": r.days_until_due, "recipient": r.recipient, "status": r.status,
            "sent_at": r.sent_at, "created_at": r.created_at,
        }
        for r in result.scalars().all()
    ]
