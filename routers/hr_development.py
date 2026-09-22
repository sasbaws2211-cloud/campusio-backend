"""Staff onboarding, training, and certification compliance endpoints."""
from datetime import datetime, date, timedelta
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user, require_permission
from database import get_session
from dependencies import assert_campus_access
from models.hr_development import (
    OnboardingTaskCreate, OnboardingTaskStatus, OnboardingTaskStatusUpdate,
    StaffCertification, StaffCertificationCreate, StaffOnboardingTask, StaffTraining,
    StaffTrainingCreate, StaffTrainingImpactUpdate,
)
from models.staff import Staff, StaffStatus
from models.user import User
from services.hr_development_service import seed_default_onboarding_checklist

router = APIRouter(prefix="/hr/development", tags=["HR Development"])


def school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


async def verify_staff(staff_id: str, user: User, session: AsyncSession) -> None:
    staff = (await session.execute(select(Staff).where(Staff.id == staff_id, Staff.school_id == school_id(user)))).scalar_one_or_none()
    if not staff:
        raise HTTPException(status_code=404, detail="Staff member not found")
    assert_campus_access(user, staff.campus_id)


@router.get("/onboarding", response_model=list[dict])
async def list_onboarding(staff_id: str | None = None, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    query = select(StaffOnboardingTask).where(StaffOnboardingTask.school_id == school_id(user))
    if staff_id:
        query = query.where(StaffOnboardingTask.staff_id == staff_id)
    result = await session.execute(query.order_by(StaffOnboardingTask.due_date))
    return [item.model_dump() for item in result.scalars().all()]


@router.post("/onboarding", response_model=dict)
async def create_onboarding(payload: OnboardingTaskCreate, user: User = Depends(require_permission("hr.onboarding.manage")), session: AsyncSession = Depends(get_session)):
    await verify_staff(payload.staff_id, user, session)
    item = StaffOnboardingTask(school_id=school_id(user), created_by=user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.patch("/onboarding/{task_id}", response_model=dict)
async def update_onboarding(task_id: str, payload: OnboardingTaskStatusUpdate, user: User = Depends(require_permission("hr.onboarding.manage")), session: AsyncSession = Depends(get_session)):
    if payload.status not in (OnboardingTaskStatus.PENDING, OnboardingTaskStatus.COMPLETED, OnboardingTaskStatus.WAIVED):
        raise HTTPException(status_code=422, detail="Invalid onboarding task status")
    result = await session.execute(select(StaffOnboardingTask).where(StaffOnboardingTask.id == task_id, StaffOnboardingTask.school_id == school_id(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Onboarding task not found")
    item.status = payload.status
    item.completed_at = datetime.utcnow() if payload.status == OnboardingTaskStatus.COMPLETED else None
    item.completed_by = user.id if payload.status == OnboardingTaskStatus.COMPLETED else None
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.get("/onboarding/overdue", response_model=list[dict])
async def list_overdue_onboarding(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """Onboarding tasks with a due_date in the past that are still pending
    — previously due_date existed only for sorting the plain list, with no
    equivalent to this same file's own /certifications/expiring or
    /staff/probation/upcoming "here's what needs attention" pattern."""
    today = date.today().isoformat()
    result = await session.execute(
        select(StaffOnboardingTask).where(
            StaffOnboardingTask.school_id == school_id(user),
            StaffOnboardingTask.status == OnboardingTaskStatus.PENDING,
            StaffOnboardingTask.due_date.is_not(None),
            StaffOnboardingTask.due_date < today,
        ).order_by(StaffOnboardingTask.due_date)
    )
    return [item.model_dump() for item in result.scalars().all()]


@router.post("/onboarding/seed-default/{staff_id}", response_model=dict)
async def seed_default_onboarding(staff_id: str, user: User = Depends(require_permission("hr.onboarding.manage")), session: AsyncSession = Depends(get_session)):
    """Creates the standard onboarding checklist for a staff member who
    didn't come through the hire-applicant pipeline (which does this
    automatically) — e.g. an existing staff member added directly, or a
    school just turning this feature on. Safe to re-run."""
    await verify_staff(staff_id, user, session)
    created = await seed_default_onboarding_checklist(session, school_id(user), staff_id, created_by=user.id)
    await session.commit()
    return {"created": created}


@router.get("/training", response_model=list[dict])
async def list_training(staff_id: str | None = None, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    query = select(StaffTraining).where(StaffTraining.school_id == school_id(user))
    if staff_id:
        query = query.where(StaffTraining.staff_id == staff_id)
    result = await session.execute(query.order_by(StaffTraining.completion_date.desc()))
    return [item.model_dump() for item in result.scalars().all()]


@router.post("/training", response_model=dict)
async def create_training(payload: StaffTrainingCreate, user: User = Depends(require_permission("hr.training.manage")), session: AsyncSession = Depends(get_session)):
    await verify_staff(payload.staff_id, user, session)
    item = StaffTraining(school_id=school_id(user), recorded_by=user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.patch("/training/{training_id}/impact", response_model=dict)
async def update_training_impact(training_id: str, payload: StaffTrainingImpactUpdate, user: User = Depends(require_permission("hr.training.manage")), session: AsyncSession = Depends(get_session)):
    """Filled in after the fact, once there's been time to observe whether
    the training actually changed anything — separate from create_training
    since cost is known up front but impact never is."""
    if payload.performance_impact_rating < 1 or payload.performance_impact_rating > 5:
        raise HTTPException(status_code=422, detail="performance_impact_rating must be between 1 and 5")
    result = await session.execute(select(StaffTraining).where(StaffTraining.id == training_id, StaffTraining.school_id == school_id(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Training record not found")
    item.performance_impact_rating = payload.performance_impact_rating
    item.roi_notes = payload.roi_notes
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.get("/training/roi-summary", response_model=dict)
async def training_roi_summary(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """Aggregate cost vs. average observed impact — the roll-up view
    behind "was training spend worth it", not per-record detail."""
    result = await session.execute(select(StaffTraining).where(StaffTraining.school_id == school_id(user)))
    records = result.scalars().all()
    total_cost = sum(float(r.cost or 0) for r in records)
    rated = [r for r in records if r.performance_impact_rating is not None]
    avg_impact = round(sum(r.performance_impact_rating for r in rated) / len(rated), 2) if rated else None
    return {
        "total_records": len(records),
        "total_cost": round(total_cost, 2),
        "records_with_cost": len([r for r in records if r.cost is not None]),
        "records_rated_for_impact": len(rated),
        "average_impact_rating": avg_impact,
    }


@router.get("/training/cpd-compliance", response_model=list[dict])
async def cpd_compliance(year: int | None = None, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """Completed CPD hours vs. Staff.annual_cpd_hours_required for `year`
    (defaults to the current year) — previously training was logged
    after the fact with no target or completion-tracking at all; a GES-
    style "N hours of CPD per year" obligation was unenforceable. Only
    reports staff who actually have a requirement set (None is the
    default for every existing staff row, so a school not using this
    feature sees an empty list, not a false shortfall)."""
    target_year = year or date.today().year
    staff_result = await session.execute(
        select(Staff).where(
            Staff.school_id == school_id(user), Staff.status == StaffStatus.ACTIVE,
            Staff.annual_cpd_hours_required.is_not(None),
        )
    )
    staff_list = staff_result.scalars().all()
    if not staff_list:
        return []

    staff_ids = [s.id for s in staff_list]
    training_result = await session.execute(
        select(StaffTraining).where(
            StaffTraining.school_id == school_id(user), StaffTraining.staff_id.in_(staff_ids),
            StaffTraining.completion_date.is_not(None),
            StaffTraining.completion_date.like(f"{target_year}%"),
        )
    )
    hours_by_staff: dict[str, float] = {}
    for t in training_result.scalars().all():
        hours_by_staff[t.staff_id] = hours_by_staff.get(t.staff_id, 0.0) + (t.hours or 0.0)

    return [
        {
            "staff_id": s.id, "name": f"{s.first_name} {s.last_name}", "year": target_year,
            "hours_required": s.annual_cpd_hours_required,
            "hours_completed": round(hours_by_staff.get(s.id, 0.0), 1),
            "met_requirement": hours_by_staff.get(s.id, 0.0) >= s.annual_cpd_hours_required,
        }
        for s in staff_list
    ]


@router.get("/certifications", response_model=list[dict])
async def list_certifications(staff_id: str | None = None, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    query = select(StaffCertification).where(StaffCertification.school_id == school_id(user))
    if staff_id:
        query = query.where(StaffCertification.staff_id == staff_id)
    result = await session.execute(query.order_by(StaffCertification.expiry_date))
    return [item.model_dump() for item in result.scalars().all()]


@router.get("/certifications/expiring", response_model=list[dict])
async def expiring_certifications(days: int = 30, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    if days < 0 or days > 3650:
        raise HTTPException(status_code=422, detail="Days must be between 0 and 3650")
    cutoff = (date.today() + timedelta(days=days)).isoformat()
    result = await session.execute(select(StaffCertification).where(StaffCertification.school_id == school_id(user), StaffCertification.expiry_date != None, StaffCertification.expiry_date <= cutoff).order_by(StaffCertification.expiry_date))
    return [item.model_dump() for item in result.scalars().all()]


@router.post("/certifications", response_model=dict)
async def create_certification(payload: StaffCertificationCreate, user: User = Depends(require_permission("hr.certification.create")), session: AsyncSession = Depends(get_session)):
    await verify_staff(payload.staff_id, user, session)
    item = StaffCertification(school_id=school_id(user), recorded_by=user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()