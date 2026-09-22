"""Recruitment and hiring lifecycle for school staff."""
import secrets
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlmodel import SQLModel, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user, require_permission
from database import get_session
from models.hr_recruitment import (
    ApplicantStatusUpdate, Interview, InterviewCreate, InterviewResultUpdate,
    OfferCreate, OfferLetter, OfferStatus, OfferStatusUpdate, StaffApplicant,
    StaffApplicantCreate, StaffApplicantStatus, Vacancy, VacancyCreate, VacancyUpdate,
)
from models.staff import Staff, StaffType
from models.school import School
from models.user import User, UserRole
from models.payroll import PayrollContract, PaySchedule
from services.hr_development_service import seed_default_onboarding_checklist

router = APIRouter(prefix="/hr/recruitment", tags=["HR Recruitment"])


class HireApplicantRequest(SQLModel):
    """The applicant record only carries what's needed to run a hiring
    pipeline (name/email/phone/qualification) — creating an actual Staff
    record needs a few more fields a school always requires (DOB, gender,
    staff type, position, start date), so this endpoint collects those and
    creates the Staff row directly from the applicant's own details."""
    date_of_birth: str
    gender: str
    staff_type: StaffType
    position: str
    department: Optional[str] = None
    date_joined: str
    staff_id: Optional[str] = None
    campus_id: Optional[str] = None
    role: Optional[UserRole] = None
    # None = fall back to the vacancy's own employment_type (if this
    # applicant came through one) — previously Vacancy.employment_type was
    # simply dropped at hire, since Staff had no field to receive it at all.
    employment_type: Optional[str] = None
    contract_end_date: Optional[str] = None
    # Setting up pay terms was previously a fully separate, easily-forgotten
    # manual step (POST /payroll/contracts) — a new hire could go unpaid
    # for a cycle simply because nobody remembered. When given, a
    # PayrollContract is created in the same call; when omitted, no
    # contract is created (unchanged from before) and the response flags
    # that explicitly via payroll_contract_created=false rather than
    # silently saying nothing.
    create_payroll_contract: bool = True
    basic_salary: Optional[float] = None  # Falls back to the accepted OfferLetter's salary, if any
    pay_schedule: PaySchedule = PaySchedule.MONTHLY


def _school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


def _vacancy_dict(item: Vacancy) -> dict:
    return {**item.model_dump(), "status": item.status.value}


def _applicant_dict(item: StaffApplicant) -> dict:
    return {**item.model_dump(), "status": item.status.value}


def _interview_dict(item: Interview) -> dict:
    return {**item.model_dump(), "result": item.result.value}


def _offer_dict(item: OfferLetter) -> dict:
    return {**item.model_dump(), "status": item.status.value}


@router.get("/vacancies", response_model=list[dict])
async def list_vacancies(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Vacancy).where(Vacancy.school_id == _school_id(user)).order_by(Vacancy.created_at.desc()))
    return [_vacancy_dict(item) for item in result.scalars().all()]


@router.post("/vacancies", response_model=dict)
async def create_vacancy(payload: VacancyCreate, user: User = Depends(require_permission("hr.vacancy.manage")), session: AsyncSession = Depends(get_session)):
    if payload.positions < 1:
        raise HTTPException(status_code=422, detail="Positions must be at least one")
    item = Vacancy(school_id=_school_id(user), created_by=user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _vacancy_dict(item)


@router.patch("/vacancies/{vacancy_id}", response_model=dict)
async def update_vacancy(vacancy_id: str, payload: VacancyUpdate, user: User = Depends(require_permission("hr.vacancy.manage")), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Vacancy).where(Vacancy.id == vacancy_id, Vacancy.school_id == _school_id(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Vacancy not found")
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("positions", 1) < 1:
        raise HTTPException(status_code=422, detail="Positions must be at least one")
    for key, value in changes.items():
        setattr(item, key, value)
    item.updated_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _vacancy_dict(item)


@router.get("/applicants", response_model=list[dict])
async def list_applicants(status: StaffApplicantStatus | None = None, user: User = Depends(require_permission("hr.applicant.manage")), session: AsyncSession = Depends(get_session)):
    query = select(StaffApplicant).where(StaffApplicant.school_id == _school_id(user))
    if status:
        query = query.where(StaffApplicant.status == status)
    result = await session.execute(query.order_by(StaffApplicant.created_at.desc()))
    return [_applicant_dict(item) for item in result.scalars().all()]


@router.post("/applicants", response_model=dict)
async def create_applicant(payload: StaffApplicantCreate, user: User = Depends(require_permission("hr.applicant.manage")), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(user)
    if payload.vacancy_id:
        vacancy = (await session.execute(select(Vacancy).where(Vacancy.id == payload.vacancy_id, Vacancy.school_id == school_id))).scalar_one_or_none()
        if not vacancy:
            raise HTTPException(status_code=404, detail="Vacancy not found")
    item = StaffApplicant(school_id=school_id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _applicant_dict(item)


APPLICANT_STATUS_TRANSITIONS = {
    StaffApplicantStatus.APPLIED: {StaffApplicantStatus.SCREENING, StaffApplicantStatus.REJECTED, StaffApplicantStatus.WITHDRAWN},
    StaffApplicantStatus.SCREENING: {StaffApplicantStatus.INTERVIEW, StaffApplicantStatus.REJECTED, StaffApplicantStatus.WITHDRAWN},
    StaffApplicantStatus.INTERVIEW: {StaffApplicantStatus.OFFERED, StaffApplicantStatus.REJECTED, StaffApplicantStatus.WITHDRAWN},
    StaffApplicantStatus.OFFERED: {StaffApplicantStatus.HIRED, StaffApplicantStatus.REJECTED, StaffApplicantStatus.WITHDRAWN},
    # HIRED/REJECTED/WITHDRAWN are terminal via this endpoint — hire_applicant
    # is the real next step once HIRED, and re-opening a rejected/withdrawn
    # application isn't a flow this pipeline supports today.
}


@router.patch("/applicants/{applicant_id}/status", response_model=dict)
async def update_applicant_status(applicant_id: str, payload: ApplicantStatusUpdate, user: User = Depends(require_permission("hr.applicant.manage")), session: AsyncSession = Depends(get_session)):
    """Direct status transitions only move one pipeline stage forward (or
    to REJECTED/WITHDRAWN) at a time — previously any status could be set
    from any other with no transition table at all, letting a caller skip
    straight from APPLIED to HIRED, bypassing the interview/offer pipeline
    and its own audit trail entirely."""
    result = await session.execute(select(StaffApplicant).where(StaffApplicant.id == applicant_id, StaffApplicant.school_id == _school_id(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Staff applicant not found")
    if payload.status != item.status and payload.status not in APPLICANT_STATUS_TRANSITIONS.get(item.status, set()):
        raise HTTPException(status_code=400, detail=f"Cannot move applicant from {item.status.value} to {payload.status.value}")
    item.status = payload.status
    if payload.notes is not None:
        item.notes = payload.notes
    item.updated_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _applicant_dict(item)


@router.get("/interviews", response_model=list[dict])
async def list_interviews(user: User = Depends(require_permission("hr.interview.manage")), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Interview).where(Interview.school_id == _school_id(user)).order_by(Interview.scheduled_at))
    return [_interview_dict(item) for item in result.scalars().all()]


@router.post("/interviews", response_model=dict)
async def create_interview(payload: InterviewCreate, user: User = Depends(require_permission("hr.interview.manage")), session: AsyncSession = Depends(get_session)):
    applicant = (await session.execute(select(StaffApplicant).where(StaffApplicant.id == payload.applicant_id, StaffApplicant.school_id == _school_id(user)))).scalar_one_or_none()
    if not applicant:
        raise HTTPException(status_code=404, detail="Staff applicant not found")
    applicant.status = StaffApplicantStatus.INTERVIEW
    applicant.updated_at = datetime.utcnow()
    item = Interview(school_id=_school_id(user), created_by=user.id, **payload.model_dump())
    session.add_all([applicant, item])
    await session.commit()
    await session.refresh(item)
    return _interview_dict(item)


@router.patch("/interviews/{interview_id}", response_model=dict)
async def update_interview(interview_id: str, payload: InterviewResultUpdate, user: User = Depends(require_permission("hr.interview.manage")), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Interview).where(Interview.id == interview_id, Interview.school_id == _school_id(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Interview not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    applicant = (await session.execute(select(StaffApplicant).where(StaffApplicant.id == item.applicant_id, StaffApplicant.school_id == _school_id(user)))).scalar_one_or_none()
    if applicant and item.result == "pass":
        applicant.status = StaffApplicantStatus.OFFERED
        session.add(applicant)
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _interview_dict(item)


@router.get("/offers", response_model=list[dict])
async def list_offers(user: User = Depends(require_permission("hr.offer.manage")), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(OfferLetter).where(OfferLetter.school_id == _school_id(user)).order_by(OfferLetter.created_at.desc()))
    return [_offer_dict(item) for item in result.scalars().all()]


@router.post("/offers", response_model=dict)
async def create_offer(payload: OfferCreate, user: User = Depends(require_permission("hr.offer.manage")), session: AsyncSession = Depends(get_session)):
    applicant = (await session.execute(select(StaffApplicant).where(StaffApplicant.id == payload.applicant_id, StaffApplicant.school_id == _school_id(user)))).scalar_one_or_none()
    if not applicant:
        raise HTTPException(status_code=404, detail="Staff applicant not found")
    applicant.status = StaffApplicantStatus.OFFERED
    item = OfferLetter(school_id=_school_id(user), created_by=user.id, **payload.model_dump())
    session.add_all([applicant, item])
    await session.commit()
    await session.refresh(item)
    return _offer_dict(item)


@router.patch("/offers/{offer_id}/status", response_model=dict)
async def update_offer_status(offer_id: str, payload: OfferStatusUpdate, user: User = Depends(require_permission("hr.offer.manage")), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(OfferLetter).where(OfferLetter.id == offer_id, OfferLetter.school_id == _school_id(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Offer letter not found")
    item.status = payload.status
    item.updated_at = datetime.utcnow()
    if payload.status == OfferStatus.ACCEPTED:
        applicant = (await session.execute(select(StaffApplicant).where(StaffApplicant.id == item.applicant_id, StaffApplicant.school_id == _school_id(user)))).scalar_one_or_none()
        if applicant:
            applicant.status = StaffApplicantStatus.HIRED
            session.add(applicant)
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _offer_dict(item)


@router.post("/applicants/{applicant_id}/hire", response_model=dict)
async def hire_applicant(applicant_id: str, payload: HireApplicantRequest, user: User = Depends(require_permission("hr.applicant.hire")), session: AsyncSession = Depends(get_session)):
    """Complete the recruitment pipeline: turn a HIRED applicant into an
    actual Staff record and link the two (StaffApplicant.hired_staff_id) so
    the hire is traceable back to the vacancy/interview/offer that produced
    it. This is the step nothing else in the pipeline performs — accepting
    an offer only flips the applicant's status, it doesn't provision staff."""
    school_id = _school_id(user)
    applicant = (await session.execute(select(StaffApplicant).where(StaffApplicant.id == applicant_id, StaffApplicant.school_id == school_id))).scalar_one_or_none()
    if not applicant:
        raise HTTPException(status_code=404, detail="Staff applicant not found")
    if applicant.status != StaffApplicantStatus.HIRED:
        raise HTTPException(status_code=400, detail="Only applicants marked 'hired' can be converted to a staff record")
    if applicant.hired_staff_id:
        raise HTTPException(status_code=400, detail="This applicant has already been converted to a staff record")

    staff_id_code = payload.staff_id or f"STF-{datetime.now().year}-{secrets.token_hex(3).upper()}"
    existing = (await session.execute(select(Staff).where(Staff.school_id == school_id, Staff.staff_id == staff_id_code))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Staff ID already exists")

    vacancy_for_employment_type = None
    if applicant.vacancy_id and payload.employment_type is None:
        vacancy_for_employment_type = (await session.execute(select(Vacancy).where(Vacancy.id == applicant.vacancy_id, Vacancy.school_id == school_id))).scalar_one_or_none()

    staff = Staff(
        school_id=school_id,
        staff_id=staff_id_code,
        first_name=applicant.first_name,
        last_name=applicant.last_name,
        email=applicant.email,
        phone=applicant.phone or "",
        date_of_birth=payload.date_of_birth,
        gender=payload.gender,
        staff_type=payload.staff_type,
        position=payload.position,
        department=payload.department,
        qualification=applicant.qualification,
        date_joined=payload.date_joined,
        campus_id=payload.campus_id,
        role=payload.role,
        employment_type=payload.employment_type or (vacancy_for_employment_type.employment_type if vacancy_for_employment_type else "permanent"),
        contract_end_date=payload.contract_end_date,
    )
    session.add(staff)
    await session.flush()
    applicant.hired_staff_id = staff.id
    applicant.updated_at = datetime.utcnow()
    session.add(applicant)

    # Vacancy.positions was never decremented on hire, so workforce-planning
    # figures silently drifted from reality — a vacancy for 1 position stayed
    # showing 1 open position forever, even after it was filled.
    if applicant.vacancy_id:
        vacancy = (await session.execute(select(Vacancy).where(Vacancy.id == applicant.vacancy_id, Vacancy.school_id == school_id))).scalar_one_or_none()
        if vacancy and vacancy.positions > 0:
            vacancy.positions -= 1
            vacancy.updated_at = datetime.utcnow()
            session.add(vacancy)

    await session.flush()
    await seed_default_onboarding_checklist(session, school_id, staff.id, created_by=user.id)

    payroll_contract_created = False
    payroll_contract_warning = None
    if payload.create_payroll_contract:
        basic_salary = payload.basic_salary
        if basic_salary is None:
            offer_result = await session.execute(
                select(OfferLetter).where(
                    OfferLetter.applicant_id == applicant.id, OfferLetter.status == OfferStatus.ACCEPTED,
                    OfferLetter.salary.is_not(None),
                ).order_by(OfferLetter.updated_at.desc())
            )
            accepted_offer = offer_result.scalars().first()
            if accepted_offer:
                basic_salary = accepted_offer.salary

        if basic_salary is not None and basic_salary > 0:
            contract = PayrollContract(
                school_id=school_id,
                staff_id=staff.id,
                basic_salary=basic_salary,
                pay_schedule=payload.pay_schedule,
                effective_from=datetime.strptime(payload.date_joined, "%Y-%m-%d") if len(payload.date_joined) == 10 else datetime.utcnow(),
                created_by=user.id,
                notes=f"Auto-created at hire from applicant {applicant.id}",
            )
            session.add(contract)
            payroll_contract_created = True
        else:
            payroll_contract_warning = (
                "No basic_salary was given and no accepted offer with a salary exists for this applicant — "
                "no payroll contract was created. Set one up via POST /payroll/contracts before the first payroll run."
            )

    await session.commit()
    await session.refresh(staff)
    response = {
        "staff_id": staff.id, "staff_code": staff.staff_id, "applicant": _applicant_dict(applicant),
        "payroll_contract_created": payroll_contract_created,
    }
    if payroll_contract_warning:
        response["payroll_contract_warning"] = payroll_contract_warning
    return response


@router.get("/offers/{offer_id}/pdf")
async def download_offer_letter_pdf(offer_id: str, user: User = Depends(require_permission("hr.offer.manage")), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(user)
    offer = (await session.execute(select(OfferLetter).where(OfferLetter.id == offer_id, OfferLetter.school_id == school_id))).scalar_one_or_none()
    if not offer:
        raise HTTPException(status_code=404, detail="Offer letter not found")
    applicant = (await session.execute(select(StaffApplicant).where(StaffApplicant.id == offer.applicant_id))).scalar_one_or_none()
    school = (await session.execute(select(School).where(School.id == school_id))).scalar_one_or_none()

    from services.certificate_pdf_service import CertificatePDFService
    html = f"""
    <html><body style="font-family: Helvetica, sans-serif; padding: 40px;">
      <h2 style="margin-bottom: 0;">{school.name if school else ''}</h2>
      <p style="color: #555; margin-top: 4px;">Offer of Employment</p>
      <hr/>
      <p>Date: {datetime.utcnow().strftime('%d %B %Y')}</p>
      <p>Dear {applicant.first_name if applicant else ''} {applicant.last_name if applicant else ''},</p>
      <p>We are pleased to offer you the position of <b>{offer.position}</b>
      {f'with a starting salary of GHS {offer.salary:,.2f}' if offer.salary else ''},
      commencing {offer.start_date or 'on a date to be agreed'}.</p>
      {f'<p><b>Terms:</b> {offer.terms}</p>' if offer.terms else ''}
      {f'<p>This offer is valid until {offer.expiry_date}.</p>' if offer.expiry_date else ''}
      <p>Please confirm your acceptance of this offer at your earliest convenience.</p>
      <p style="margin-top: 60px;">Sincerely,<br/>{school.name if school else 'The School'}</p>
    </body></html>
    """
    pdf_bytes = CertificatePDFService().generate_pdf(template_name="offer_letter_inline", data={}, custom_html=html)
    filename = f"offer_letter_{(applicant.last_name if applicant else offer_id)}.pdf"
    return Response(content=pdf_bytes, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename={filename}"})