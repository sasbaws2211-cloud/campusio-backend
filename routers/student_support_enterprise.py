"""Restricted safeguarding, SEN, counselling, behaviour, and consent APIs."""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_roles, get_current_user
from database import get_session
from models.student import Student
from models.student_support import StudentSupportCase, SupportCaseSeverity, is_safeguarding_case
from models.student_support_enterprise import (
    BehaviourSupportPlan, BehaviourSupportPlanCreate, CounsellingSession, CounsellingSessionCreate,
    IEPCreate, IndividualEducationPlan, IEPReview, IEPReviewCreate,
    ParentConsent, ParentConsentCreate, ParentConsentDecision,
    SENProfile, SENProfileCreate, SupportEscalation, SupportEscalationCreate, ConsentStatus,
    SafeguardingReferral, SafeguardingReferralCreate, SafeguardingReferralStatusUpdate,
)
from models.user import User, UserRole
from services.audit_service import log_event

router = APIRouter(prefix="/student-support/secure", tags=["Sensitive Student Support"])
# General behaviour plans / escalations / consents — lower sensitivity than
# SEN or counselling, kept at the module's original coarse gate (plus the
# new safeguarding-lead role, additive).
CASE_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR, UserRole.NURSE, UserRole.SAFEGUARDING_LEAD)
SAFEGUARDING_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.SAFEGUARDING_LEAD)
# SEN profiles/IEPs carry diagnosis-level detail — HR and the clinic nurse
# no longer get blanket access; a dedicated SEN coordinator does.
SEN_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.SEN_COORDINATOR)
# Counselling notes are the most sensitive record type here. Admins/
# safeguarding leads get full oversight of every session; a counselor only
# ever sees their own — see list_counselling/create_counselling below.
COUNSELLING_OVERSIGHT_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.SAFEGUARDING_LEAD)
COUNSELLING_WRITE_ROLES = COUNSELLING_OVERSIGHT_ROLES + (UserRole.COUNSELOR,)


def scope(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


async def check_student(student_id: str, user: User, session: AsyncSession):
    result = await session.execute(select(Student).where(Student.id == student_id, Student.school_id == scope(user)))
    student = result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")


async def audit(session: AsyncSession, user: User, action: str, entity_type: str, entity_id: str, summary: str):
    await log_event(session, user, action, entity_type, summary, entity_id=entity_id, school_id=user.school_id)


@router.get("/sen-profiles", response_model=list[dict])
async def list_sen_profiles(student_id: str | None = None, user: User = Depends(require_roles(*SEN_ROLES)), session: AsyncSession = Depends(get_session)):
    query = select(SENProfile).where(SENProfile.school_id == scope(user))
    if student_id:
        query = query.where(SENProfile.student_id == student_id)
    return [item.model_dump() for item in (await session.execute(query.order_by(SENProfile.updated_at.desc()))).scalars().all()]


@router.post("/sen-profiles", response_model=dict)
async def create_sen_profile(payload: SENProfileCreate, user: User = Depends(require_roles(*SEN_ROLES)), session: AsyncSession = Depends(get_session)):
    await check_student(payload.student_id, user, session)
    item = SENProfile(school_id=scope(user), created_by=user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    await audit(session, user, "student_support.sen_profile.created", "sen_profile", item.id, "Created SEN profile")
    return item.model_dump()


@router.get("/ieps", response_model=list[dict])
async def list_ieps(student_id: str | None = None, user: User = Depends(require_roles(*SEN_ROLES)), session: AsyncSession = Depends(get_session)):
    query = select(IndividualEducationPlan).where(IndividualEducationPlan.school_id == scope(user))
    if student_id:
        query = query.where(IndividualEducationPlan.student_id == student_id)
    return [item.model_dump() for item in (await session.execute(query.order_by(IndividualEducationPlan.review_date))).scalars().all()]


@router.post("/ieps", response_model=dict)
async def create_iep(payload: IEPCreate, user: User = Depends(require_roles(*SEN_ROLES)), session: AsyncSession = Depends(get_session)):
    await check_student(payload.student_id, user, session)
    if payload.sen_profile_id:
        sen_profile = (await session.execute(select(SENProfile).where(SENProfile.id == payload.sen_profile_id, SENProfile.school_id == scope(user)))).scalar_one_or_none()
        if not sen_profile or sen_profile.student_id != payload.student_id:
            raise HTTPException(status_code=400, detail="SEN profile not found for this student")
    item = IndividualEducationPlan(school_id=scope(user), created_by=user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    await audit(session, user, "student_support.iep.created", "iep", item.id, "Created individual education plan")
    return item.model_dump()


async def _find_iep(iep_id: str, user: User, session: AsyncSession) -> IndividualEducationPlan:
    item = (await session.execute(select(IndividualEducationPlan).where(IndividualEducationPlan.id == iep_id, IndividualEducationPlan.school_id == scope(user)))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Individual education plan not found")
    return item


@router.get("/ieps/{iep_id}/reviews", response_model=list[dict])
async def list_iep_reviews(iep_id: str, user: User = Depends(require_roles(*SEN_ROLES)), session: AsyncSession = Depends(get_session)):
    await _find_iep(iep_id, user, session)
    result = await session.execute(select(IEPReview).where(IEPReview.iep_id == iep_id, IEPReview.school_id == scope(user)).order_by(IEPReview.review_date.desc()))
    return [item.model_dump() for item in result.scalars().all()]


@router.post("/ieps/{iep_id}/reviews", response_model=dict)
async def create_iep_review(iep_id: str, payload: IEPReviewCreate, user: User = Depends(require_roles(*SEN_ROLES)), session: AsyncSession = Depends(get_session)):
    iep = await _find_iep(iep_id, user, session)
    item = IEPReview(school_id=scope(user), iep_id=iep_id, reviewed_by=user.id, **payload.model_dump())
    session.add(item)
    # The IEP's own review_date is "when is the next review due" — keep it
    # current so list_ieps' order_by(review_date) still surfaces plans
    # needing attention soonest, rather than freezing at the original date
    # forever once reviews start being logged.
    iep.review_date = payload.next_review_date or iep.review_date
    iep.updated_at = datetime.utcnow()
    session.add(iep)
    await session.commit()
    await session.refresh(item)
    await audit(session, user, "student_support.iep.reviewed", "iep", iep_id, "Logged IEP review")
    return item.model_dump()


@router.get("/counselling", response_model=list[dict])
async def list_counselling(student_id: str | None = None, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    if user.role not in COUNSELLING_WRITE_ROLES:
        raise HTTPException(status_code=403, detail="Access denied. Counselling records are restricted to counselors and safeguarding-cleared staff")
    query = select(CounsellingSession).where(CounsellingSession.school_id == scope(user))
    if student_id:
        query = query.where(CounsellingSession.student_id == student_id)
    # A counselor sees only their own sessions — not a colleague's client
    # notes — even though they can see the module at all. Admins and
    # safeguarding leads get full oversight of every counsellor's sessions.
    if user.role not in COUNSELLING_OVERSIGHT_ROLES:
        query = query.where(CounsellingSession.counsellor_id == user.id)
    return [item.model_dump() for item in (await session.execute(query.order_by(CounsellingSession.session_date.desc()))).scalars().all()]


@router.post("/counselling", response_model=dict)
async def create_counselling(payload: CounsellingSessionCreate, user: User = Depends(require_roles(*COUNSELLING_WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    await check_student(payload.student_id, user, session)
    item = CounsellingSession(school_id=scope(user), counsellor_id=user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    await audit(session, user, "student_support.counselling.created", "counselling_session", item.id, "Created confidential counselling session")
    return item.model_dump()


@router.get("/behaviour-plans", response_model=list[dict])
async def list_behaviour_plans(student_id: str | None = None, user: User = Depends(require_roles(*CASE_ROLES)), session: AsyncSession = Depends(get_session)):
    query = select(BehaviourSupportPlan).where(BehaviourSupportPlan.school_id == scope(user))
    if student_id:
        query = query.where(BehaviourSupportPlan.student_id == student_id)
    return [item.model_dump() for item in (await session.execute(query.order_by(BehaviourSupportPlan.created_at.desc()))).scalars().all()]


@router.post("/behaviour-plans", response_model=dict)
async def create_behaviour_plan(payload: BehaviourSupportPlanCreate, user: User = Depends(require_roles(*CASE_ROLES)), session: AsyncSession = Depends(get_session)):
    await check_student(payload.student_id, user, session)
    item = BehaviourSupportPlan(school_id=scope(user), created_by=user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    await audit(session, user, "student_support.behaviour_plan.created", "behaviour_plan", item.id, "Created behaviour support plan")
    return item.model_dump()


@router.get("/escalations", response_model=list[dict])
async def list_escalations(case_id: str | None = None, user: User = Depends(require_roles(*CASE_ROLES)), session: AsyncSession = Depends(get_session)):
    query = select(SupportEscalation, StudentSupportCase).join(StudentSupportCase, StudentSupportCase.id == SupportEscalation.case_id).where(SupportEscalation.school_id == scope(user))
    if case_id:
        query = query.where(SupportEscalation.case_id == case_id)
    can_see_safeguarding = user.role in SAFEGUARDING_ROLES
    result = await session.execute(query.order_by(SupportEscalation.created_at.desc()))
    return [escalation.model_dump() for escalation, case in result.all() if can_see_safeguarding or not is_safeguarding_case(case.case_type)]


@router.post("/escalations", response_model=dict)
async def create_escalation(payload: SupportEscalationCreate, user: User = Depends(require_roles(*CASE_ROLES)), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(StudentSupportCase).where(StudentSupportCase.id == payload.case_id, StudentSupportCase.school_id == scope(user)))
    case = result.scalar_one_or_none()
    if not case:
        raise HTTPException(status_code=404, detail="Support case not found")
    if is_safeguarding_case(case.case_type) and user.role not in SAFEGUARDING_ROLES:
        raise HTTPException(status_code=403, detail="This case is restricted to safeguarding-cleared staff")
    try:
        to_severity = SupportCaseSeverity(payload.to_severity)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid escalation severity")
    item = SupportEscalation(school_id=scope(user), from_severity=case.severity.value, created_by=user.id, to_severity=to_severity.value, case_id=payload.case_id, reason=payload.reason, referred_to=payload.referred_to)
    case.severity = to_severity
    case.updated_at = datetime.utcnow()
    session.add_all([item, case])
    await session.commit()
    await session.refresh(item)
    await audit(session, user, "student_support.case.escalated", "support_case", case.id, "Escalated support case")
    return item.model_dump()


async def _find_safeguarding_case(case_id: str, user: User, session: AsyncSession) -> StudentSupportCase:
    """Only ever used by the referral endpoints below, so both the
    "does this case exist" and "is it actually a safeguarding case" checks
    live in one place — a referral against a non-safeguarding case_type
    makes no sense (that's what SupportEscalation's referred_to is for)."""
    case = (await session.execute(select(StudentSupportCase).where(StudentSupportCase.id == case_id, StudentSupportCase.school_id == scope(user)))).scalar_one_or_none()
    if not case:
        raise HTTPException(status_code=404, detail="Support case not found")
    if not is_safeguarding_case(case.case_type):
        raise HTTPException(status_code=400, detail="Statutory referrals only apply to safeguarding cases")
    return case


@router.get("/safeguarding/referrals", response_model=list[dict])
async def list_safeguarding_referrals(case_id: str | None = None, user: User = Depends(require_roles(*SAFEGUARDING_ROLES)), session: AsyncSession = Depends(get_session)):
    query = select(SafeguardingReferral).where(SafeguardingReferral.school_id == scope(user))
    if case_id:
        query = query.where(SafeguardingReferral.case_id == case_id)
    result = await session.execute(query.order_by(SafeguardingReferral.referral_date.desc()))
    return [{**item.model_dump(), "status": item.status.value} for item in result.scalars().all()]


@router.post("/safeguarding/referrals", response_model=dict)
async def create_safeguarding_referral(payload: SafeguardingReferralCreate, user: User = Depends(require_roles(*SAFEGUARDING_ROLES)), session: AsyncSession = Depends(get_session)):
    await _find_safeguarding_case(payload.case_id, user, session)
    item = SafeguardingReferral(school_id=scope(user), created_by=user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    await audit(session, user, "student_support.safeguarding_referral.created", "safeguarding_referral", item.id, "Logged statutory referral")
    return {**item.model_dump(), "status": item.status.value}


@router.patch("/safeguarding/referrals/{referral_id}/status", response_model=dict)
async def update_safeguarding_referral_status(referral_id: str, payload: SafeguardingReferralStatusUpdate, user: User = Depends(require_roles(*SAFEGUARDING_ROLES)), session: AsyncSession = Depends(get_session)):
    item = (await session.execute(select(SafeguardingReferral).where(SafeguardingReferral.id == referral_id, SafeguardingReferral.school_id == scope(user)))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Referral not found")
    item.status = payload.status
    item.notes = payload.notes or item.notes
    item.updated_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    await audit(session, user, "student_support.safeguarding_referral.updated", "safeguarding_referral", item.id, "Updated statutory referral status")
    return {**item.model_dump(), "status": item.status.value}


@router.get("/consents", response_model=list[dict])
async def list_consents(student_id: str | None = None, user: User = Depends(require_roles(*CASE_ROLES)), session: AsyncSession = Depends(get_session)):
    query = select(ParentConsent).where(ParentConsent.school_id == scope(user))
    if student_id:
        query = query.where(ParentConsent.student_id == student_id)
    return [{**item.model_dump(), "status": item.status.value} for item in (await session.execute(query.order_by(ParentConsent.created_at.desc()))).scalars().all()]


@router.post("/consents", response_model=dict)
async def create_consent(payload: ParentConsentCreate, user: User = Depends(require_roles(*CASE_ROLES)), session: AsyncSession = Depends(get_session)):
    await check_student(payload.student_id, user, session)
    item = ParentConsent(school_id=scope(user), requested_by=user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    await audit(session, user, "student_support.consent.requested", "parent_consent", item.id, "Requested parent consent")
    return {**item.model_dump(), "status": item.status.value}


@router.patch("/consents/{consent_id}", response_model=dict)
async def decide_consent(consent_id: str, payload: ParentConsentDecision, user: User = Depends(require_roles(*CASE_ROLES)), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(ParentConsent).where(ParentConsent.id == consent_id, ParentConsent.school_id == scope(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Consent record not found")
    item.status = payload.status
    item.notes = payload.notes or item.notes
    item.decided_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    await audit(session, user, "student_support.consent.decided", "parent_consent", item.id, "Updated parent consent decision")
    return {**item.model_dump(), "status": item.status.value}