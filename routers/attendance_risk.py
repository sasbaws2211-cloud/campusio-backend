"""Admin settings + manual trigger + history for proactive attendance-risk
alerting — the scheduled sweep itself lives in
services/attendance_risk_service.py and services/scheduler.py."""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_roles
from database import get_session
from models.attendance_risk import AttendanceRiskSettings, AttendanceRiskSettingsUpdate, AttendanceRiskAlert
from models.user import User, UserRole
from services.attendance_risk_service import get_or_create_settings, run_risk_sweep

router = APIRouter(prefix="/attendance-risk", tags=["Attendance Risk Alerts"])

WRITE_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)


def _school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


@router.get("/settings", response_model=dict)
async def get_settings(current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    settings = await get_or_create_settings(session, _school_id(current_user))
    await session.commit()
    return {
        "enabled": settings.enabled, "threshold_percent": settings.threshold_percent,
        "lookback_days": settings.lookback_days, "cooldown_days": settings.cooldown_days,
    }


@router.put("/settings", response_model=dict)
async def update_settings(payload: AttendanceRiskSettingsUpdate, current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    settings = await get_or_create_settings(session, _school_id(current_user))
    for field in ("enabled", "threshold_percent", "lookback_days", "cooldown_days"):
        value = getattr(payload, field)
        if value is not None:
            setattr(settings, field, value)
    from datetime import datetime
    settings.updated_at = datetime.utcnow()
    await session.commit()
    return {
        "enabled": settings.enabled, "threshold_percent": settings.threshold_percent,
        "lookback_days": settings.lookback_days, "cooldown_days": settings.cooldown_days,
    }


@router.post("/run-now", response_model=dict)
async def run_now(current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    return await run_risk_sweep(session, school_id=_school_id(current_user))


@router.get("/history", response_model=List[dict])
async def alert_history(
    student_id: Optional[str] = None,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    stmt = select(AttendanceRiskAlert).where(AttendanceRiskAlert.school_id == school_id)
    if student_id:
        stmt = stmt.where(AttendanceRiskAlert.student_id == student_id)
    result = await session.execute(stmt.order_by(AttendanceRiskAlert.created_at.desc()).limit(100))
    return [
        {
            "id": a.id, "student_id": a.student_id, "attendance_rate": a.attendance_rate, "channel": a.channel,
            "recipient": a.recipient, "status": a.status, "sent_at": a.sent_at, "created_at": a.created_at,
        }
        for a in result.scalars().all()
    ]
