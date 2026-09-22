"""Staff performance review workflow."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import SQLModel, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user, require_roles
from database import get_session
from dependencies import assert_campus_access
from models.hr import (
    StaffPerformanceReview, StaffPerformanceReviewCreate, StaffPerformanceReviewUpdate, ReviewStatus,
    SelfAssessmentSubmit, BulkLaunchReviewsRequest,
)
from models.staff_performance_plus import (
    StaffGoal, StaffGoalCreate, StaffGoalProgressUpdate,
    PerformanceFeedbackRequest, PerformanceFeedbackRequestCreate,
    PerformanceFeedback, PerformanceFeedbackSubmit,
    PerformanceImprovementPlan, PerformanceImprovementPlanCreate,
    PerformanceImprovementPlanCheckIn, PerformanceImprovementPlanClose,
)
from models.staff import Staff, StaffType
from models.hr_admin import StaffDisciplinaryAction
from models.user import User, UserRole

router = APIRouter(prefix="/hr", tags=["Human Resources"])
WRITE_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR)


class StatusRequest(SQLModel):
    status: ReviewStatus


def _school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


async def _own_staff_id(user: User, session: AsyncSession) -> str | None:
    """The caller's own linked Staff record, if any — None rather than
    raising, since plain staff (not just admin/HR) can hit these endpoints
    and most callers won't have one (parents, students)."""
    result = await session.execute(select(Staff).where(Staff.school_id == _school_id(user), Staff.user_id == user.id))
    staff = result.scalar_one_or_none()
    if not staff:
        result = await session.execute(select(Staff).where(Staff.school_id == _school_id(user), Staff.email == user.email))
        staff = result.scalar_one_or_none()
    return staff.id if staff else None


def _review_dict(review: StaffPerformanceReview, staff: Staff) -> dict:
    return {
        "id": review.id, "staff_id": review.staff_id, "staff_name": f"{staff.first_name} {staff.last_name}",
        "manager_id": staff.manager_id,
        "review_period": review.review_period, "status": review.status.value,
        "self_rating": review.self_rating, "self_comments": review.self_comments,
        "self_submitted_at": review.self_submitted_at.isoformat() if review.self_submitted_at else None,
        "overall_rating": review.overall_rating, "strengths": review.strengths,
        "development_goals": review.development_goals, "training_needs": review.training_needs,
        "reviewer_id": review.reviewer_id,
        "submitted_at": review.submitted_at.isoformat() if review.submitted_at else None,
        "acknowledged_at": review.acknowledged_at.isoformat() if review.acknowledged_at else None,
    }


async def _get_review(review_id: str, user: User, session: AsyncSession):
    result = await session.execute(select(StaffPerformanceReview, Staff).join(Staff, Staff.id == StaffPerformanceReview.staff_id).where(StaffPerformanceReview.id == review_id, StaffPerformanceReview.school_id == _school_id(user)))
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Performance review not found")
    return row[0], row[1]


async def _require_can_manage_review(current_user: User, staff: Staff, session: AsyncSession) -> None:
    """Who's allowed to write the manager's side of a review: admin/HR (as
    before), or — now that a manager hierarchy exists — the staff member
    this review's subject actually reports to. Without this, manager_id
    would just be a display field with no functional teeth.

    Checked before the WRITE_ROLES bypass: an admin/HR user is never allowed
    to manage their own review, even though they'd otherwise pass — without
    this, the same person could write, submit, and acknowledge both sides
    of their own performance review with no independent sign-off at all."""
    assert_campus_access(current_user, staff.campus_id)
    own_id = await _own_staff_id(current_user, session)
    if own_id and own_id == staff.id:
        raise HTTPException(status_code=403, detail="You cannot manage your own performance review — ask another admin or your manager's manager")
    if current_user.role in WRITE_ROLES:
        return
    if own_id and own_id == staff.manager_id:
        return
    raise HTTPException(status_code=403, detail="Only this staff member's manager, or HR/an admin, can do that")


@router.get("/performance-reviews", response_model=list[dict])
async def list_reviews(staff_id: str | None = None, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """Admin/HR see every review in the school (optionally filtered to one
    staff member). Everyone else — a plain staff member with no HR role —
    only ever sees their own review(s) plus their direct reports' (staff
    whose manager_id points back at them), never the whole school's."""
    query = select(StaffPerformanceReview, Staff).join(Staff, Staff.id == StaffPerformanceReview.staff_id).where(StaffPerformanceReview.school_id == _school_id(current_user))
    if staff_id:
        query = query.where(StaffPerformanceReview.staff_id == staff_id)

    if current_user.role not in WRITE_ROLES:
        own_id = await _own_staff_id(current_user, session)
        if not own_id:
            return []
        query = query.where((StaffPerformanceReview.staff_id == own_id) | (Staff.manager_id == own_id))

    result = await session.execute(query.order_by(StaffPerformanceReview.created_at.desc()))
    return [_review_dict(review, staff) for review, staff in result.all()]


@router.get("/performance-reviews/{review_id}", response_model=dict)
async def get_review(review_id: str, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """Single-review fetch — also attaches a `teacher_effectiveness` score
    (services.teacher_effectiveness_service, for the school's CURRENT
    academic term) when the subject is teaching staff, so whoever's
    writing this review can actually see it. Previously that service was
    real but only ever surfaced on a standalone executive-analytics
    endpoint — a manager writing a review had no way to pull it in."""
    review, staff = await _get_review(review_id, current_user, session)
    if current_user.role not in WRITE_ROLES:
        own_id = await _own_staff_id(current_user, session)
        if not own_id or (own_id != review.staff_id and own_id != staff.manager_id):
            raise HTTPException(status_code=403, detail="Access denied")

    result = _review_dict(review, staff)
    if staff.staff_type == StaffType.TEACHING:
        from routers.timetable import get_current_term_id
        from services.teacher_effectiveness_service import compute_teacher_effectiveness
        current_term_id = await get_current_term_id(session, _school_id(current_user))
        if current_term_id:
            effectiveness = await compute_teacher_effectiveness(session, _school_id(current_user), current_term_id, staff_id=staff.id)
            result["teacher_effectiveness"] = effectiveness["teachers"][0] if effectiveness.get("teachers") else None
    return result


@router.post("/performance-reviews", response_model=dict)
async def create_review(payload: StaffPerformanceReviewCreate, current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    staff = (await session.execute(select(Staff).where(Staff.id == payload.staff_id, Staff.school_id == school_id))).scalar_one_or_none()
    if not staff:
        raise HTTPException(status_code=400, detail="Staff member not found in this school")
    review = StaffPerformanceReview(school_id=school_id, reviewer_id=current_user.id, **payload.model_dump())
    session.add(review)
    await session.commit()
    await session.refresh(review)
    return _review_dict(review, staff)


@router.post("/performance-reviews/bulk-launch", response_model=dict)
async def bulk_launch_reviews(payload: BulkLaunchReviewsRequest, current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    """Start a review cycle for a batch of staff at once — one DRAFT review
    per staff member for this review_period. Staff already reviewed for
    this exact period are skipped rather than duplicated."""
    school_id = _school_id(current_user)
    created = []
    skipped = []
    for staff_id in payload.staff_ids:
        staff = (await session.execute(select(Staff).where(Staff.id == staff_id, Staff.school_id == school_id))).scalar_one_or_none()
        if not staff:
            skipped.append({"staff_id": staff_id, "reason": "Staff not found"})
            continue
        existing = (await session.execute(
            select(StaffPerformanceReview).where(
                StaffPerformanceReview.school_id == school_id,
                StaffPerformanceReview.staff_id == staff_id,
                StaffPerformanceReview.review_period == payload.review_period,
            )
        )).scalar_one_or_none()
        if existing:
            skipped.append({"staff_id": staff_id, "reason": "Already has a review for this period"})
            continue
        review = StaffPerformanceReview(
            school_id=school_id, staff_id=staff_id, review_period=payload.review_period, reviewer_id=current_user.id,
        )
        session.add(review)
        created.append(staff_id)

    await session.commit()
    return {
        "review_period": payload.review_period,
        "created": created, "skipped": skipped,
        "created_count": len(created), "skipped_count": len(skipped),
    }


@router.put("/performance-reviews/{review_id}", response_model=dict)
async def update_review(review_id: str, payload: StaffPerformanceReviewUpdate, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    review, staff = await _get_review(review_id, current_user, session)
    await _require_can_manage_review(current_user, staff, session)
    if review.status not in (ReviewStatus.DRAFT, ReviewStatus.SELF_ASSESSED):
        raise HTTPException(status_code=400, detail="Only draft or self-assessed reviews can be edited")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(review, key, value)
    review.updated_at = datetime.utcnow()
    session.add(review)
    await session.commit()
    return _review_dict(review, staff)


@router.put("/performance-reviews/{review_id}/self-assessment", response_model=dict)
async def submit_self_assessment(review_id: str, payload: SelfAssessmentSubmit, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """The staff member the review is about fills in their own rating and
    comments — never the manager. Only usable before the manager has
    submitted their side, so a self-assessment can't be silently rewritten
    after the review has moved on."""
    review, staff = await _get_review(review_id, current_user, session)
    own_id = await _own_staff_id(current_user, session)
    if not own_id or own_id != review.staff_id:
        raise HTTPException(status_code=403, detail="You can only submit a self-assessment for your own review")
    if review.status not in (ReviewStatus.DRAFT, ReviewStatus.SELF_ASSESSED):
        raise HTTPException(status_code=400, detail="This review has already been submitted by your manager and can no longer be self-assessed")

    review.self_rating = payload.self_rating
    review.self_comments = payload.self_comments
    review.self_submitted_at = datetime.utcnow()
    review.status = ReviewStatus.SELF_ASSESSED
    review.updated_at = datetime.utcnow()
    session.add(review)
    await session.commit()
    return _review_dict(review, staff)


@router.put("/performance-reviews/{review_id}/acknowledge", response_model=dict)
async def acknowledge_review(review_id: str, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """The staff member the review is about confirms they've read their
    manager's completed review — the other half of the sign-off besides
    self-assessment, kept separate from the admin/HR status endpoint below
    so 'acknowledged' actually means the staff person saw it, not that an
    admin closed it out on their behalf."""
    review, staff = await _get_review(review_id, current_user, session)
    own_id = await _own_staff_id(current_user, session)
    if not own_id or own_id != review.staff_id:
        raise HTTPException(status_code=403, detail="You can only acknowledge your own review")
    if review.status != ReviewStatus.SUBMITTED:
        raise HTTPException(status_code=400, detail="Only a submitted review can be acknowledged")

    review.status = ReviewStatus.ACKNOWLEDGED
    review.acknowledged_at = datetime.utcnow()
    review.updated_at = datetime.utcnow()
    session.add(review)
    await session.commit()
    return _review_dict(review, staff)


LOW_RATING_THRESHOLD = 2  # overall_rating <= this flags requires_pip_consideration on submit


@router.post("/performance-reviews/{review_id}/status", response_model=dict)
async def change_review_status(review_id: str, payload: StatusRequest, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    review, staff = await _get_review(review_id, current_user, session)
    await _require_can_manage_review(current_user, staff, session)
    transitions = {
        ReviewStatus.DRAFT: {ReviewStatus.SUBMITTED},
        ReviewStatus.SELF_ASSESSED: {ReviewStatus.SUBMITTED},
        ReviewStatus.SUBMITTED: {ReviewStatus.ACKNOWLEDGED},
    }
    if payload.status not in transitions.get(review.status, set()):
        raise HTTPException(status_code=400, detail=f"Cannot change review from {review.status.value} to {payload.status.value}")
    review.status = payload.status
    review.updated_at = datetime.utcnow()
    if payload.status == ReviewStatus.SUBMITTED:
        review.submitted_at = datetime.utcnow()
    else:
        review.acknowledged_at = datetime.utcnow()
    session.add(review)
    await session.commit()

    result = _review_dict(review, staff)
    # A low rating previously just sat as a record nobody was prompted to
    # act on. This never auto-creates a PIP (writing real goals/dates is a
    # human judgment call) — it only flags that one might be warranted,
    # the same "surface, don't auto-act" pattern used for the exit-cascade
    # warnings and the promotion-review flag elsewhere in this codebase.
    if payload.status == ReviewStatus.SUBMITTED and review.overall_rating is not None and review.overall_rating <= LOW_RATING_THRESHOLD:
        active_pip = (await session.execute(
            select(PerformanceImprovementPlan).where(
                PerformanceImprovementPlan.school_id == _school_id(current_user),
                PerformanceImprovementPlan.staff_id == review.staff_id,
                PerformanceImprovementPlan.status == "active",
            )
        )).scalar_one_or_none()
        result["requires_pip_consideration"] = active_pip is None
    return result


# ==================== Individual staff goals (OKRs) ====================

async def _require_can_manage_staff(current_user: User, staff_id: str, session: AsyncSession) -> Optional[str]:
    """Same authorization shape as _require_can_manage_review, generalized
    to any staff member rather than a specific review — WRITE_ROLES always
    pass; otherwise the caller must be that staff member's manager, or the
    staff member themselves (goals/PIP check-ins are things you also do to
    your own record, unlike a manager's review write). Returns the
    caller's own staff id (or None) for the "is this about me" checks."""
    staff = (await session.execute(select(Staff).where(Staff.id == staff_id, Staff.school_id == _school_id(current_user)))).scalar_one_or_none()
    if staff:
        assert_campus_access(current_user, staff.campus_id)
    own_id = await _own_staff_id(current_user, session)
    if current_user.role in WRITE_ROLES:
        return own_id
    if own_id == staff_id:
        return own_id
    if staff and own_id and own_id == staff.manager_id:
        return own_id
    raise HTTPException(status_code=403, detail="Only this staff member, their manager, or HR/an admin can do that")


@router.get("/staff-goals", response_model=list[dict])
async def list_staff_goals(staff_id: str | None = None, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    query = select(StaffGoal).where(StaffGoal.school_id == _school_id(current_user))
    if staff_id:
        query = query.where(StaffGoal.staff_id == staff_id)
    # A second, independent `if` (not `elif`) so the ownership restriction
    # below always applies for a non-privileged caller, whether or not they
    # passed staff_id — passing someone else's staff_id must not bypass it.
    if current_user.role not in WRITE_ROLES:
        own_id = await _own_staff_id(current_user, session)
        if not own_id:
            return []
        reports_result = await session.execute(select(Staff.id).where(Staff.manager_id == own_id))
        report_ids = [row[0] for row in reports_result.all()]
        query = query.where(StaffGoal.staff_id.in_([own_id, *report_ids]))
    result = await session.execute(query.order_by(StaffGoal.created_at.desc()))
    return [item.model_dump() for item in result.scalars().all()]


@router.post("/staff-goals", response_model=dict)
async def create_staff_goal(payload: StaffGoalCreate, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    await _require_can_manage_staff(current_user, payload.staff_id, session)
    item = StaffGoal(school_id=_school_id(current_user), created_by=current_user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.patch("/staff-goals/{goal_id}/progress", response_model=dict)
async def update_staff_goal_progress(goal_id: str, payload: StaffGoalProgressUpdate, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(StaffGoal).where(StaffGoal.id == goal_id, StaffGoal.school_id == _school_id(current_user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Staff goal not found")
    await _require_can_manage_staff(current_user, item.staff_id, session)
    changes = payload.model_dump(exclude_unset=True)
    for key, value in changes.items():
        setattr(item, key, value)
    item.updated_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


# ==================== 360-degree feedback ====================

@router.post("/performance-reviews/{review_id}/feedback-requests", response_model=dict)
async def request_360_feedback(review_id: str, payload: PerformanceFeedbackRequestCreate, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    review, staff = await _get_review(review_id, current_user, session)
    await _require_can_manage_review(current_user, staff, session)

    created = []
    for giver_id in payload.feedback_giver_staff_ids:
        giver = (await session.execute(select(Staff).where(Staff.id == giver_id, Staff.school_id == _school_id(current_user)))).scalar_one_or_none()
        if not giver:
            continue
        req = PerformanceFeedbackRequest(
            school_id=_school_id(current_user), review_id=review_id, subject_staff_id=review.staff_id,
            feedback_giver_staff_id=giver_id, relationship=payload.relationship, requested_by=current_user.id,
        )
        session.add(req)
        created.append(giver_id)
    await session.commit()
    return {"requested_from": created, "count": len(created)}


@router.get("/feedback-requests/my", response_model=list[dict])
async def list_my_feedback_requests(current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """The caller's own queue: feedback they've been asked to give but
    haven't submitted yet."""
    own_id = await _own_staff_id(current_user, session)
    if not own_id:
        return []
    result = await session.execute(
        select(PerformanceFeedbackRequest).where(
            PerformanceFeedbackRequest.school_id == _school_id(current_user),
            PerformanceFeedbackRequest.feedback_giver_staff_id == own_id,
        ).order_by(PerformanceFeedbackRequest.created_at.desc())
    )
    requests = result.scalars().all()
    submitted_result = await session.execute(
        select(PerformanceFeedback.request_id).where(PerformanceFeedback.request_id.in_([r.id for r in requests]))
    )
    submitted_ids = {row[0] for row in submitted_result.all()}
    return [{**r.model_dump(), "submitted": r.id in submitted_ids} for r in requests]


@router.post("/feedback-requests/{request_id}/submit", response_model=dict)
async def submit_360_feedback(request_id: str, payload: PerformanceFeedbackSubmit, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    own_id = await _own_staff_id(current_user, session)
    result = await session.execute(select(PerformanceFeedbackRequest).where(PerformanceFeedbackRequest.id == request_id, PerformanceFeedbackRequest.school_id == _school_id(current_user)))
    req = result.scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=404, detail="Feedback request not found")
    if not own_id or own_id != req.feedback_giver_staff_id:
        raise HTTPException(status_code=403, detail="This feedback request wasn't addressed to you")

    existing = (await session.execute(select(PerformanceFeedback).where(PerformanceFeedback.request_id == request_id))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Feedback has already been submitted for this request")

    if payload.rating is not None and not (1 <= payload.rating <= 5):
        raise HTTPException(status_code=422, detail="rating must be between 1 and 5")

    item = PerformanceFeedback(school_id=_school_id(current_user), request_id=request_id, **payload.model_dump())
    session.add(item)
    await session.commit()
    return {"message": "Feedback submitted"}


@router.get("/performance-reviews/{review_id}/feedback", response_model=dict)
async def get_360_feedback(review_id: str, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """Aggregated view for the reviewer/HR — deliberately omits which
    giver said what (only their relationship to the subject), so peer/
    subordinate feedback can be honest without fear of attribution."""
    review, staff = await _get_review(review_id, current_user, session)
    await _require_can_manage_review(current_user, staff, session)

    requests_result = await session.execute(select(PerformanceFeedbackRequest).where(PerformanceFeedbackRequest.review_id == review_id))
    requests = requests_result.scalars().all()
    feedback_result = await session.execute(
        select(PerformanceFeedback).where(PerformanceFeedback.request_id.in_([r.id for r in requests]))
    ) if requests else None
    feedback_by_request = {f.request_id: f for f in (feedback_result.scalars().all() if feedback_result else [])}

    entries = []
    ratings = []
    for req in requests:
        fb = feedback_by_request.get(req.id)
        if not fb:
            continue
        if fb.rating is not None:
            ratings.append(fb.rating)
        entries.append({
            "relationship": req.relationship, "rating": fb.rating, "strengths": fb.strengths,
            "areas_for_improvement": fb.areas_for_improvement, "comments": fb.comments,
        })

    return {
        "requested": len(requests), "submitted": len(entries),
        "average_rating": round(sum(ratings) / len(ratings), 2) if ratings else None,
        "entries": entries,
    }


# ==================== Performance Improvement Plans ====================

@router.get("/performance-improvement-plans", response_model=list[dict])
async def list_pips(staff_id: str | None = None, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    query = select(PerformanceImprovementPlan).where(PerformanceImprovementPlan.school_id == _school_id(current_user))
    if staff_id:
        query = query.where(PerformanceImprovementPlan.staff_id == staff_id)
    # A second, independent `if` (not `elif`) so the ownership restriction
    # below always applies for a non-privileged caller, whether or not they
    # passed staff_id — passing someone else's staff_id must not bypass it.
    if current_user.role not in WRITE_ROLES:
        own_id = await _own_staff_id(current_user, session)
        if not own_id:
            return []
        reports_result = await session.execute(select(Staff.id).where(Staff.manager_id == own_id))
        report_ids = [row[0] for row in reports_result.all()]
        query = query.where(PerformanceImprovementPlan.staff_id.in_([own_id, *report_ids]))
    result = await session.execute(query.order_by(PerformanceImprovementPlan.created_at.desc()))
    return [item.model_dump() for item in result.scalars().all()]


@router.post("/performance-improvement-plans", response_model=dict)
async def create_pip(payload: PerformanceImprovementPlanCreate, current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    staff = (await session.execute(select(Staff).where(Staff.id == payload.staff_id, Staff.school_id == _school_id(current_user)))).scalar_one_or_none()
    if not staff:
        raise HTTPException(status_code=400, detail="Staff member not found in this school")
    item = PerformanceImprovementPlan(school_id=_school_id(current_user), created_by=current_user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.patch("/performance-improvement-plans/{pip_id}/check-in", response_model=dict)
async def check_in_pip(pip_id: str, payload: PerformanceImprovementPlanCheckIn, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(PerformanceImprovementPlan).where(PerformanceImprovementPlan.id == pip_id, PerformanceImprovementPlan.school_id == _school_id(current_user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Performance improvement plan not found")
    await _require_can_manage_staff(current_user, item.staff_id, session)
    if item.status != "active":
        raise HTTPException(status_code=400, detail=f"Cannot check in on a plan in {item.status} status")
    stamp = datetime.utcnow().strftime("%Y-%m-%d")
    item.check_in_notes = f"{item.check_in_notes}\n\n[{stamp}] {payload.check_in_notes}" if item.check_in_notes else f"[{stamp}] {payload.check_in_notes}"
    item.updated_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.patch("/performance-improvement-plans/{pip_id}/close", response_model=dict)
async def close_pip(pip_id: str, payload: PerformanceImprovementPlanClose, current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    if payload.status not in ("completed", "escalated", "closed"):
        raise HTTPException(status_code=422, detail="status must be completed, escalated, or closed")
    result = await session.execute(select(PerformanceImprovementPlan).where(PerformanceImprovementPlan.id == pip_id, PerformanceImprovementPlan.school_id == _school_id(current_user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Performance improvement plan not found")
    item.status = payload.status
    item.outcome = payload.outcome
    item.closed_at = datetime.utcnow()
    item.updated_at = datetime.utcnow()
    session.add(item)

    # Previously "escalated" was just a label — closing a PIP that way
    # created nothing else, so the case existed only inside this one
    # record with no entry point into the disciplinary process a real
    # escalation implies. This gives HR a starting record instead of
    # nothing; it's deliberately "open" status, not pre-resolved, since
    # the actual disciplinary outcome is still HR's call to make.
    disciplinary_action_id = None
    if payload.status == "escalated":
        action = StaffDisciplinaryAction(
            school_id=_school_id(current_user), staff_id=item.staff_id,
            incident_date=datetime.utcnow().strftime("%Y-%m-%d"),
            action_type="Performance Improvement Plan Escalation",
            description=f"PIP {item.id} escalated. Reason: {item.reason}. Outcome: {payload.outcome or 'not recorded'}.",
            recorded_by=current_user.id,
        )
        session.add(action)
        await session.flush()
        disciplinary_action_id = action.id

    await session.commit()
    await session.refresh(item)
    response = item.model_dump()
    if disciplinary_action_id:
        response["disciplinary_action_id"] = disciplinary_action_id
    return response
