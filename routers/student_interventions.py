"""Structured student intervention plans and progress history."""
from datetime import date
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_roles
from database import get_session
from models.student import Student
from models.student_support import InterventionPlan, InterventionPlanCreate, InterventionProgress, InterventionProgressCreate, StudentSupportCase
from models.user import User, UserRole

router = APIRouter(prefix="/student-support", tags=["Student Interventions"])
ACCESS_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR, UserRole.NURSE, UserRole.SAFEGUARDING_LEAD)


def scope(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


@router.get("/intervention-plans", response_model=list[dict])
async def list_plans(case_id: str | None = None, user: User = Depends(require_roles(*ACCESS_ROLES)), session: AsyncSession = Depends(get_session)):
    query = select(InterventionPlan).where(InterventionPlan.school_id == scope(user))
    if case_id:
        query = query.where(InterventionPlan.case_id == case_id)
    result = await session.execute(query.order_by(InterventionPlan.created_at.desc()))
    return [item.model_dump() for item in result.scalars().all()]


@router.post("/intervention-plans", response_model=dict)
async def create_plan(payload: InterventionPlanCreate, user: User = Depends(require_roles(*ACCESS_ROLES)), session: AsyncSession = Depends(get_session)):
    case = (await session.execute(select(StudentSupportCase).where(StudentSupportCase.id == payload.case_id, StudentSupportCase.school_id == scope(user)))).scalar_one_or_none()
    if not case:
        raise HTTPException(status_code=404, detail="Support case not found")
    item = InterventionPlan(school_id=scope(user), created_by=user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.get("/intervention-plans/{plan_id}/progress", response_model=list[dict])
async def list_progress(plan_id: str, user: User = Depends(require_roles(*ACCESS_ROLES)), session: AsyncSession = Depends(get_session)):
    plan = (await session.execute(select(InterventionPlan).where(InterventionPlan.id == plan_id, InterventionPlan.school_id == scope(user)))).scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Intervention plan not found")
    result = await session.execute(select(InterventionProgress).where(InterventionProgress.plan_id == plan_id, InterventionProgress.school_id == scope(user)).order_by(InterventionProgress.created_at.desc()))
    return [item.model_dump() for item in result.scalars().all()]


@router.post("/intervention-plans/{plan_id}/progress", response_model=dict)
async def add_progress(plan_id: str, payload: InterventionProgressCreate, user: User = Depends(require_roles(*ACCESS_ROLES)), session: AsyncSession = Depends(get_session)):
    plan = (await session.execute(select(InterventionPlan).where(InterventionPlan.id == plan_id, InterventionPlan.school_id == scope(user)))).scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Intervention plan not found")
    item = InterventionProgress(school_id=scope(user), plan_id=plan_id, recorded_by=user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.get("/intervention-summary", response_model=dict)
async def intervention_summary(user: User = Depends(require_roles(*ACCESS_ROLES)), session: AsyncSession = Depends(get_session)):
    plans = (await session.execute(select(InterventionPlan).where(InterventionPlan.school_id == scope(user)))).scalars().all()
    progress = (await session.execute(select(InterventionProgress).where(InterventionProgress.school_id == scope(user)))).scalars().all()
    percentages = [item.progress_percent for item in progress if item.progress_percent is not None]
    today = date.today().isoformat()
    return {"plans_total": len(plans), "plans_active": sum(1 for plan in plans if plan.status == "active"), "plans_completed": sum(1 for plan in plans if plan.status == "completed"), "reviews_due": sum(1 for plan in plans if plan.status == "active" and plan.review_date and plan.review_date <= today), "progress_updates": len(progress), "average_progress_percent": round(sum(percentages) / len(percentages), 1) if percentages else None}