"""Enterprise admissions workflow endpoints."""
from collections import defaultdict
from datetime import datetime
import json
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlmodel import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_roles
from config import get_settings
from database import get_session
from models.admissions import Applicant, ApplicantDocument, ApplicationStatus, RejectionReasonCode, WithdrawalReasonCode
from models.classroom import Class
from models.school import School, AcademicTerm
from models.admissions_enterprise import (
    AdmissionDeposit, AdmissionDepositCreate, AdmissionDepositPayment, AdmissionDepositStatus, AdmissionDepositWaive,
    ApplicantInterview, ApplicantInterviewCreate, ApplicantInterviewUpdate,
    ApplicantOffer, ApplicantOfferCreate, ApplicantOfferStatusUpdate, AdmissionOfferStatus,
    ApplicantStageEvent, ApplicantStageChange, WaitlistReorderRequest,
    EntranceExamResult, EntranceExamResultCreate,
    ApplicationReview, ApplicationReviewCreate,
)
from models.user import User, UserRole
from services.email_service import email_service

router = APIRouter(prefix="/admissions/enterprise", tags=["Admissions Enterprise"])
WRITE_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.REGISTRAR)


def scope(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


async def applicant(applicant_id: str, user: User, session: AsyncSession) -> Applicant:
    result = await session.execute(select(Applicant).where(Applicant.id == applicant_id, Applicant.school_id == scope(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Applicant not found")
    return item


async def _send_stage_email(email: str, name: str, stage: str) -> None:
    await email_service.send_email(
        to=[email],
        subject=f"Admissions update: {stage.replace('_', ' ').title()}",
        html_body=f"<p>Dear {name},</p><p>Your admissions application status is now <strong>{stage.replace('_', ' ').title()}</strong>.</p><p>Please contact the school admissions office if you have questions.</p>",
        text_body=f"Your admissions application status is now {stage.replace('_', ' ').title()}.",
    )


async def _send_notice_email(email: str, name: str, subject: str, message: str, link: str | None = None) -> None:
    link_html = f"<p><a href=\"{link}\">Click here</a></p>" if link else ""
    link_text = f"\n{link}" if link else ""
    await email_service.send_email(
        to=[email],
        subject=subject,
        html_body=f"<p>Dear {name},</p><p>{message}</p>{link_html}<p>Please contact the school admissions office if you have questions.</p>",
        text_body=f"{message}{link_text}",
    )


async def _advance_applicant_stage(item: Applicant, new_status: ApplicationStatus, reason: str | None, user: User, session: AsyncSession, reason_code: str | None = None) -> str:
    """Core of a coarse-grained ApplicationStatus transition — status +
    reason fields + waitlist rank + the audit-trail ApplicantStageEvent.
    Shared by the dedicated stage-change endpoint and any other action that
    also moves an applicant's stage (e.g. creating an offer), so every
    stage change is logged and emailed the same way rather than some
    callers mutating item.status directly and silently skipping both.

    Blocks any further stage change once an applicant has actually been
    converted to a Student (routers/admissions.py::convert_applicant sets
    both status=ENROLLED and converted_student_id together) — without
    this, change_stage/create_offer could silently flip an already-
    enrolled applicant back to REJECTED/WITHDRAWN/OFFERED while the real,
    active Student record sits completely untouched, corrupting the audit
    trail and desyncing admissions analytics from what actually happened."""
    if item.converted_student_id:
        raise HTTPException(status_code=409, detail="This applicant has already been converted to a student and can no longer change admissions stage")

    old_status_enum = item.status
    old_status = old_status_enum.value

    # Computed BEFORE item.status is mutated below: session.execute()
    # autoflushes pending changes first, so if this ran after the mutation
    # the COUNT would already include this very item's now-WAITLISTED
    # status, off-by-one-ing everyone's rank.
    new_waitlist_rank = None
    if new_status == ApplicationStatus.WAITLISTED:
        new_waitlist_rank = (await session.execute(select(func.count(Applicant.id)).where(Applicant.school_id == scope(user), Applicant.status == ApplicationStatus.WAITLISTED))).scalar() + 1

    item.status = new_status
    if new_status == ApplicationStatus.REJECTED:
        item.rejection_reason = reason
        item.rejection_reason_code = reason_code
    elif new_status == ApplicationStatus.WITHDRAWN:
        item.withdrawal_reason = reason
        item.withdrawal_reason_code = reason_code
    elif new_status == ApplicationStatus.WAITLISTED:
        item.waitlist_rank = new_waitlist_rank

    # Leaving the waitlist via any path (promoted, offered directly,
    # rejected, withdrawn, ...) closes the rank gap for whoever was behind
    # them, so waitlist_rank stays a contiguous 1..N — the ordering the
    # GET /waitlist and reorder endpoints both rely on.
    if old_status_enum == ApplicationStatus.WAITLISTED and new_status != ApplicationStatus.WAITLISTED:
        departing_rank = item.waitlist_rank
        item.waitlist_rank = None
        if departing_rank is not None:
            behind = (await session.execute(select(Applicant).where(Applicant.school_id == scope(user), Applicant.status == ApplicationStatus.WAITLISTED, Applicant.waitlist_rank > departing_rank))).scalars().all()
            for other in behind:
                other.waitlist_rank -= 1
                session.add(other)

    item.updated_at = datetime.utcnow()
    session.add(item)
    session.add(ApplicantStageEvent(school_id=scope(user), applicant_id=item.id, from_status=old_status, to_status=new_status.value, reason=reason, reason_code=reason_code, changed_by=user.id))
    return old_status


# Linear progression through the pipeline — used only to decide whether an
# automatic trigger (scheduling an interview, recording an exam result)
# should carry the applicant forward a stage. Deliberately excludes
# REJECTED/WAITLISTED/WITHDRAWN: those are side exits, not points on this
# line, so an automatic trigger firing for an applicant sitting in one of
# them is a no-op rather than silently pulling them back into the pipeline.
PIPELINE_ORDER = [
    ApplicationStatus.INQUIRY,
    ApplicationStatus.APPLIED,
    ApplicationStatus.INTERVIEW_SCHEDULED,
    ApplicationStatus.ENTRANCE_TEST_SCHEDULED,
    ApplicationStatus.ENTRANCE_TEST_COMPLETED,
    ApplicationStatus.OFFERED,
    ApplicationStatus.ENROLLED,
]


async def _auto_advance(item: Applicant, candidate_status: ApplicationStatus, user: User, session: AsyncSession) -> bool:
    """Moves the applicant forward to `candidate_status` only if they're
    currently on the pipeline and not already at or past it — e.g.
    scheduling a second interview for an already-OFFERED applicant must not
    knock them back to INTERVIEW_SCHEDULED. Returns whether it advanced."""
    try:
        current_index = PIPELINE_ORDER.index(item.status)
        candidate_index = PIPELINE_ORDER.index(candidate_status)
    except ValueError:
        return False
    if candidate_index <= current_index:
        return False
    await _advance_applicant_stage(item, candidate_status, None, user, session)
    return True


@router.post("/applicants/{applicant_id}/stage", response_model=dict)
async def change_stage(applicant_id: str, payload: ApplicantStageChange, background_tasks: BackgroundTasks, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    item = await applicant(applicant_id, user, session)
    try:
        new_status = ApplicationStatus(payload.status)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid applicant stage")
    if new_status in (ApplicationStatus.REJECTED, ApplicationStatus.WAITLISTED, ApplicationStatus.WITHDRAWN) and not payload.reason:
        raise HTTPException(status_code=422, detail="A reason is required for rejection or waitlisting")
    if payload.reason_code:
        valid_codes = RejectionReasonCode if new_status == ApplicationStatus.REJECTED else WithdrawalReasonCode if new_status == ApplicationStatus.WITHDRAWN else None
        if valid_codes is None or payload.reason_code not in {c.value for c in valid_codes}:
            raise HTTPException(status_code=422, detail="Invalid reason code for this stage")
    await _advance_applicant_stage(item, new_status, payload.reason, user, session, reason_code=payload.reason_code)
    await session.commit()
    if item.guardian_email:
        background_tasks.add_task(_send_stage_email, item.guardian_email, item.guardian_name, new_status.value)
    return {"applicant_id": item.id, "status": item.status.value, "reason": payload.reason, "reason_code": payload.reason_code}


@router.get("/applicants/{applicant_id}/stage-history", response_model=list[dict])
async def stage_history(applicant_id: str, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    await applicant(applicant_id, user, session)
    result = await session.execute(select(ApplicantStageEvent).where(ApplicantStageEvent.applicant_id == applicant_id, ApplicantStageEvent.school_id == scope(user)).order_by(ApplicantStageEvent.created_at))
    return [item.model_dump() for item in result.scalars().all()]


async def _waitlist_rows(user: User, session: AsyncSession) -> list[dict]:
    result = await session.execute(select(Applicant).where(Applicant.school_id == scope(user), Applicant.status == ApplicationStatus.WAITLISTED).order_by(Applicant.waitlist_rank))
    return [{"rank": item.waitlist_rank, "applicant_id": item.id, "name": f"{item.first_name} {item.last_name}", "class_id": item.applying_for_class_id, "updated_at": item.updated_at} for item in result.scalars().all()]


@router.get("/waitlist", response_model=list[dict])
async def waitlist(user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    return await _waitlist_rows(user, session)


@router.post("/waitlist/{applicant_id}/promote", response_model=dict)
async def promote_from_waitlist(applicant_id: str, background_tasks: BackgroundTasks, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    """Pulls an applicant off the waitlist and back into active
    consideration (APPLIED) — a spot opened up. Deliberately doesn't jump
    straight to OFFERED: extending the actual offer still goes through
    POST /offers, same as anyone else, so every offer has a real
    ApplicantOffer record behind it. Rank cleanup for whoever was behind
    them happens inside _advance_applicant_stage."""
    item = await applicant(applicant_id, user, session)
    if item.status != ApplicationStatus.WAITLISTED:
        raise HTTPException(status_code=400, detail="Only a waitlisted applicant can be promoted")
    await _advance_applicant_stage(item, ApplicationStatus.APPLIED, "Promoted from waitlist", user, session)
    await session.commit()
    if item.guardian_email:
        background_tasks.add_task(
            _send_notice_email, item.guardian_email, item.guardian_name,
            "Good news from the waitlist",
            f"A place has opened up and {item.first_name}'s application is now back under active consideration. The school will be in touch with next steps.",
        )
    return {"applicant_id": item.id, "status": item.status.value}


@router.post("/waitlist/{applicant_id}/reorder", response_model=list[dict])
async def reorder_waitlist(applicant_id: str, payload: WaitlistReorderRequest, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    """Manually moves one applicant to a new 1-indexed position on the
    waitlist, shifting everyone else to close the gap — e.g. a sibling
    priority policy or a school-specific judgment call the auto-assigned
    append-to-end rank doesn't capture."""
    item = await applicant(applicant_id, user, session)
    if item.status != ApplicationStatus.WAITLISTED:
        raise HTTPException(status_code=400, detail="Only a waitlisted applicant can be reordered")

    ordered = (await session.execute(select(Applicant).where(Applicant.school_id == scope(user), Applicant.status == ApplicationStatus.WAITLISTED).order_by(Applicant.waitlist_rank))).scalars().all()
    ordered_ids = [a.id for a in ordered]
    ordered_ids.remove(item.id)
    new_index = max(0, min(payload.new_rank - 1, len(ordered_ids)))
    ordered_ids.insert(new_index, item.id)

    by_id = {a.id: a for a in ordered}
    for rank, aid in enumerate(ordered_ids, start=1):
        by_id[aid].waitlist_rank = rank
        session.add(by_id[aid])
    await session.commit()
    return await _waitlist_rows(user, session)


@router.get("/interviews", response_model=list[dict])
async def list_interviews(user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(ApplicantInterview).where(ApplicantInterview.school_id == scope(user)).order_by(ApplicantInterview.scheduled_at))
    return [item.model_dump() for item in result.scalars().all()]


@router.get("/exam-results", response_model=list[dict])
async def list_exam_results(applicant_id: str | None = None, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    query = select(EntranceExamResult).where(EntranceExamResult.school_id == scope(user))
    if applicant_id:
        query = query.where(EntranceExamResult.applicant_id == applicant_id)
    result = await session.execute(query.order_by(EntranceExamResult.exam_date.desc(), EntranceExamResult.subject))
    return [item.model_dump() for item in result.scalars().all()]


@router.post("/exam-results", response_model=dict)
async def create_exam_result(payload: EntranceExamResultCreate, background_tasks: BackgroundTasks, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    subject = await applicant(payload.applicant_id, user, session)
    if payload.max_score <= 0 or payload.score < 0 or payload.score > payload.max_score:
        raise HTTPException(status_code=422, detail="Score must be between zero and the maximum score")
    item = EntranceExamResult(school_id=scope(user), recorded_by=user.id, **payload.model_dump())
    session.add(item)
    advanced = await _auto_advance(subject, ApplicationStatus.ENTRANCE_TEST_COMPLETED, user, session)
    await session.commit()
    await session.refresh(item)
    if advanced and subject.guardian_email:
        background_tasks.add_task(_send_stage_email, subject.guardian_email, subject.guardian_name, ApplicationStatus.ENTRANCE_TEST_COMPLETED.value)
    return item.model_dump()


@router.post("/interviews", response_model=dict)
async def create_interview(payload: ApplicantInterviewCreate, background_tasks: BackgroundTasks, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    subject = await applicant(payload.applicant_id, user, session)
    item = ApplicantInterview(school_id=scope(user), created_by=user.id, **payload.model_dump())
    session.add(item)
    await _auto_advance(subject, ApplicationStatus.INTERVIEW_SCHEDULED, user, session)
    await session.commit()
    await session.refresh(item)
    if subject.guardian_email:
        background_tasks.add_task(
            _send_notice_email, subject.guardian_email, subject.guardian_name,
            "Admissions interview scheduled",
            f"An admissions interview has been scheduled for {subject.first_name} {subject.last_name} on <strong>{item.scheduled_at}</strong>"
            + (f" with {item.interviewer}." if item.interviewer else "."),
        )
    return item.model_dump()


@router.patch("/interviews/{interview_id}", response_model=dict)
async def update_interview(interview_id: str, payload: ApplicantInterviewUpdate, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(ApplicantInterview).where(ApplicantInterview.id == interview_id, ApplicantInterview.school_id == scope(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Interview not found")
    if payload.score is not None and (payload.score < 0 or payload.score > 100):
        raise HTTPException(status_code=422, detail="Interview score must be between 0 and 100")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    session.add(item)
    await session.commit()
    return item.model_dump()


@router.get("/offers", response_model=list[dict])
async def list_offers(user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(ApplicantOffer).where(ApplicantOffer.school_id == scope(user)).order_by(ApplicantOffer.created_at.desc()))
    return [{**item.model_dump(), "status": item.status.value} for item in result.scalars().all()]


@router.post("/offers", response_model=dict)
async def create_offer(payload: ApplicantOfferCreate, background_tasks: BackgroundTasks, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    item = await applicant(payload.applicant_id, user, session)
    # Previously had no check on item.status at all before offering (not
    # even the loose PIPELINE_ORDER check _auto_advance uses elsewhere) —
    # staff could create an offer for an applicant who is currently
    # REJECTED, WITHDRAWN, or WAITLISTED, flipping their status back to
    # OFFERED and firing a confusing/contradictory guardian notification.
    # REJECTED/WITHDRAWN/WAITLISTED aren't on PIPELINE_ORDER at all; ENROLLED
    # is on it but already blocked separately via converted_student_id.
    if item.status not in PIPELINE_ORDER or PIPELINE_ORDER.index(item.status) > PIPELINE_ORDER.index(ApplicationStatus.OFFERED):
        raise HTTPException(status_code=400, detail=f"Cannot create an offer for an applicant in {item.status.value} status")
    # class_id was previously an unvalidated free string -- nothing reads it
    # for capacity/enrollment (convert_applicant takes its own class_id
    # separately), but an offer naming a class that doesn't exist (or
    # belongs to another school) is confusing/wrong on its face, so validate
    # it the same way convert_applicant validates its own class_id.
    if payload.class_id:
        class_result = await session.execute(
            select(Class).where(Class.id == payload.class_id, Class.school_id == scope(user))
        )
        if not class_result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Class not found")
    # Route through the shared stage helper (not a direct item.status
    # assignment) so creating an offer logs an ApplicantStageEvent and
    # emails the guardian the same as any other stage change — previously
    # this bypassed both, silently.
    await _advance_applicant_stage(item, ApplicationStatus.OFFERED, None, user, session)
    offer = ApplicantOffer(school_id=scope(user), created_by=user.id, **payload.model_dump())
    session.add(offer)
    await session.commit()
    await session.refresh(offer)
    if item.guardian_email:
        background_tasks.add_task(_send_stage_email, item.guardian_email, item.guardian_name, ApplicationStatus.OFFERED.value)
    return {**offer.model_dump(), "status": offer.status.value}


@router.patch("/offers/{offer_id}/status", response_model=dict)
async def update_offer(offer_id: str, payload: ApplicantOfferStatusUpdate, background_tasks: BackgroundTasks, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(ApplicantOffer).where(ApplicantOffer.id == offer_id, ApplicantOffer.school_id == scope(user)))
    offer = result.scalar_one_or_none()
    if not offer:
        raise HTTPException(status_code=404, detail="Admission offer not found")
    transitions = {
        AdmissionOfferStatus.DRAFT: {AdmissionOfferStatus.SENT},
        AdmissionOfferStatus.SENT: {AdmissionOfferStatus.ACCEPTED, AdmissionOfferStatus.DECLINED, AdmissionOfferStatus.EXPIRED},
    }
    if payload.status not in transitions.get(offer.status, set()):
        raise HTTPException(status_code=400, detail=f"Cannot change offer from {offer.status.value} to {payload.status.value}")
    offer.status = payload.status
    offer.updated_at = datetime.utcnow()
    session.add(offer)

    item = await applicant(offer.applicant_id, user, session)
    if payload.status == AdmissionOfferStatus.DECLINED:
        # Only real applicant-pipeline change any of these transitions make:
        # a declined offer means this application is done, same as any other
        # withdrawal — so it goes through the shared helper (event + email).
        await _advance_applicant_stage(item, ApplicationStatus.WITHDRAWN, "Declined admission offer", user, session, reason_code=WithdrawalReasonCode.DECLINED_OFFER.value)
    elif item.guardian_email:
        offer_notices = {
            AdmissionOfferStatus.SENT: ("Your admission offer", f"You have been offered admission" + (f" for the {offer.offered_date} term" if offer.offered_date else "") + (f". {offer.terms}" if offer.terms else ".") + (f" This offer expires on {offer.expiry_date}." if offer.expiry_date else "")),
            AdmissionOfferStatus.ACCEPTED: ("Offer acceptance received", "Thank you for accepting the admission offer. The school will be in touch with next steps."),
            AdmissionOfferStatus.EXPIRED: ("Your admission offer has expired", "The admission offer previously extended has now expired. Please contact the school admissions office if you still wish to enroll."),
        }
        if payload.status in offer_notices:
            subject, message = offer_notices[payload.status]
            background_tasks.add_task(_send_notice_email, item.guardian_email, item.guardian_name, subject, message)

    await session.commit()
    return {**offer.model_dump(), "status": offer.status.value}


@router.get("/deposits", response_model=list[dict])
async def list_deposits(user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(AdmissionDeposit).where(AdmissionDeposit.school_id == scope(user)).order_by(AdmissionDeposit.created_at.desc()))
    return [{**item.model_dump(), "status": item.status.value, "balance": item.required_amount - item.paid_amount} for item in result.scalars().all()]


@router.post("/deposits", response_model=dict)
async def create_deposit(payload: AdmissionDepositCreate, background_tasks: BackgroundTasks, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    subject = await applicant(payload.applicant_id, user, session)
    if payload.required_amount <= 0:
        raise HTTPException(status_code=422, detail="Deposit amount must be positive")
    item = AdmissionDeposit(school_id=scope(user), recorded_by=user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    if subject.guardian_email:
        due_clause = f" by {item.due_date}" if item.due_date else ""
        school = (await session.execute(select(School).where(School.id == scope(user)))).scalar_one_or_none()
        pay_link = f"{get_settings().frontend_url}/apply/{school.code}/deposit/{item.id}" if school and school.code else None
        background_tasks.add_task(
            _send_notice_email, subject.guardian_email, subject.guardian_name,
            "Admission deposit required",
            f"A seat deposit of GHS {item.required_amount:,.2f} is required{due_clause} to secure {subject.first_name}'s admission.",
            pay_link,
        )
    return {**item.model_dump(), "status": item.status.value, "balance": item.required_amount}


@router.post("/deposits/{deposit_id}/payments", response_model=dict)
async def pay_deposit(deposit_id: str, payload: AdmissionDepositPayment, background_tasks: BackgroundTasks, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    """Manual/offline recording — cash, bank transfer, anything the school
    reconciled outside Paystack. Online card/MoMo payments go through
    routers/public_admissions.py's deposit endpoints instead, which apply
    via the same webhook pipeline as the application fee."""
    # Locked FOR UPDATE: item.paid_amount += payload.amount is a read-modify-
    # write on this endpoint's own in-memory read. Without a lock, two
    # concurrent recordings (two staff entering the same cash payment, or a
    # retried request) both read the same stale paid_amount and each add
    # their own amount to it -- the second commit overwrites the first's
    # write with a value that never included it, silently losing that
    # payment from the ledger rather than summing both. Same race shape as
    # project_campusio_admission_deposit_payment_race_fix.md's finding on
    # the sibling public/online deposit endpoint, found during a
    # verification pass over that fix and closed the same way.
    result = await session.execute(
        select(AdmissionDeposit).where(AdmissionDeposit.id == deposit_id, AdmissionDeposit.school_id == scope(user)).with_for_update()
    )
    item = result.scalar_one_or_none()
    if not item or payload.amount <= 0 or item.paid_amount + payload.amount > item.required_amount:
        raise HTTPException(status_code=400, detail="Invalid deposit payment")

    # Previously only deposit.status was checked, never the applicant's own
    # pipeline status — a REJECTED/WITHDRAWN applicant's deposit could still
    # be recorded as paid, taking money for an application that's no longer
    # active.
    subject = await applicant(item.applicant_id, user, session)
    if subject.status in (ApplicationStatus.REJECTED, ApplicationStatus.WITHDRAWN):
        raise HTTPException(status_code=400, detail=f"Cannot record a deposit payment for an applicant in {subject.status.value} status")

    item.paid_amount += payload.amount
    item.status = AdmissionDepositStatus.PAID if item.paid_amount == item.required_amount else AdmissionDepositStatus.PARTIAL
    item.updated_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    if subject.guardian_email:
        balance = item.required_amount - item.paid_amount
        balance_clause = f" A balance of GHS {balance:,.2f} remains." if balance > 0 else " This deposit is now fully paid."
        background_tasks.add_task(
            _send_notice_email, subject.guardian_email, subject.guardian_name,
            "Admission deposit payment received",
            f"A payment of GHS {payload.amount:,.2f} was recorded against {subject.first_name}'s admission deposit.{balance_clause}",
        )
    return {**item.model_dump(), "status": item.status.value, "balance": item.required_amount - item.paid_amount}


@router.patch("/deposits/{deposit_id}/waive", response_model=dict)
async def waive_deposit(deposit_id: str, payload: AdmissionDepositWaive, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(AdmissionDeposit).where(AdmissionDeposit.id == deposit_id, AdmissionDeposit.school_id == scope(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Admission deposit not found")
    if item.status == AdmissionDepositStatus.PAID:
        raise HTTPException(status_code=400, detail="A fully paid deposit cannot be waived")
    item.status = AdmissionDepositStatus.WAIVED
    item.waived_reason = payload.reason
    item.updated_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    return {**item.model_dump(), "status": item.status.value, "balance": item.required_amount - item.paid_amount}


@router.patch("/documents/{document_id}/verification", response_model=dict)
async def verify_document(document_id: str, status: str, notes: str | None = None, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    if status not in ("pending", "verified", "rejected"):
        raise HTTPException(status_code=422, detail="Invalid document verification status")
    result = await session.execute(select(ApplicantDocument).where(ApplicantDocument.id == document_id, ApplicantDocument.school_id == scope(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Applicant document not found")
    item.verification_status = status
    item.verification_notes = notes
    item.verified_by = user.id if status != "pending" else None
    item.verified_at = datetime.utcnow() if status != "pending" else None
    session.add(item)
    await session.commit()
    return item.model_dump()


@router.get("/analytics", response_model=dict)
async def admissions_analytics(user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    items = (await session.execute(select(Applicant).where(Applicant.school_id == scope(user)))).scalars().all()
    counts = {status.value: sum(1 for item in items if item.status == status) for status in ApplicationStatus}
    deposits = (await session.execute(select(AdmissionDeposit).where(AdmissionDeposit.school_id == scope(user)))).scalars().all()

    # Funnel over time — intake volume per calendar month, most recent 12.
    monthly: dict[str, int] = defaultdict(int)
    for item in items:
        monthly[item.created_at.strftime("%Y-%m")] += 1
    by_month = [{"month": month, "count": count} for month, count in sorted(monthly.items())][-12:]

    # Per-class / per-term breakdown of who's applying where.
    class_ids = {item.applying_for_class_id for item in items if item.applying_for_class_id}
    class_names = {}
    if class_ids:
        class_rows = (await session.execute(select(Class).where(Class.id.in_(class_ids), Class.school_id == scope(user)))).scalars().all()
        class_names = {c.id: c.name for c in class_rows}
    class_counts: dict[str, int] = defaultdict(int)
    for item in items:
        class_counts[class_names.get(item.applying_for_class_id, "Unspecified")] += 1
    by_class = sorted(({"class_name": k, "count": v} for k, v in class_counts.items()), key=lambda row: -row["count"])

    term_ids = {item.applying_for_term_id for item in items if item.applying_for_term_id}
    term_names = {}
    if term_ids:
        term_rows = (await session.execute(select(AcademicTerm).where(AcademicTerm.id.in_(term_ids), AcademicTerm.school_id == scope(user)))).scalars().all()
        term_names = {t.id: f"{t.academic_year} {t.term.value.title()} Term" for t in term_rows}
    term_counts: dict[str, int] = defaultdict(int)
    for item in items:
        term_counts[term_names.get(item.applying_for_term_id, "Unspecified")] += 1
    by_term = sorted(({"term_name": k, "count": v} for k, v in term_counts.items()), key=lambda row: -row["count"])

    # Interview pass rate, excluding interviews still pending a result.
    interviews = (await session.execute(select(ApplicantInterview).where(ApplicantInterview.school_id == scope(user)))).scalars().all()
    decided = [i for i in interviews if i.result in ("passed", "failed")]
    interview_pass_rate = round(sum(1 for i in decided if i.result == "passed") / len(decided) * 100, 1) if decided else None

    # Where rejections/withdrawals are actually coming from.
    reason_counts: dict[str, int] = defaultdict(int)
    for item in items:
        if item.status == ApplicationStatus.REJECTED and item.rejection_reason_code:
            reason_counts[f"Rejected — {item.rejection_reason_code.replace('_', ' ').title()}"] += 1
        elif item.status == ApplicationStatus.WITHDRAWN and item.withdrawal_reason_code:
            reason_counts[f"Withdrawn — {item.withdrawal_reason_code.replace('_', ' ').title()}"] += 1
    by_reason = sorted(({"reason": k, "count": v} for k, v in reason_counts.items()), key=lambda row: -row["count"])

    return {
        "total_applicants": len(items),
        "by_status": counts,
        "conversion_rate": round(counts["enrolled"] / len(items) * 100, 1) if items else 0.0,
        "deposit_required": sum(item.required_amount for item in deposits),
        "deposit_paid": sum(item.paid_amount for item in deposits),
        "deposit_balance": sum(item.required_amount - item.paid_amount for item in deposits),
        "by_month": by_month,
        "by_class": by_class,
        "by_term": by_term,
        "interview_pass_rate": interview_pass_rate,
        "by_reason": by_reason,
    }


def _review_dict(item: ApplicationReview) -> dict:
    return {
        "id": item.id,
        "school_id": item.school_id,
        "applicant_id": item.applicant_id,
        "reviewer_id": item.reviewer_id,
        "criteria": json.loads(item.criteria) if item.criteria else [],
        "total_score": item.total_score,
        "recommendation": item.recommendation,
        "created_at": item.created_at,
    }


@router.post("/applicants/{applicant_id}/reviews", response_model=dict)
async def create_application_review(applicant_id: str, payload: ApplicationReviewCreate, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    """Records one reviewer's scored rubric pass over an applicant. The
    weighted total is always computed here from criteria — never trusted
    from the client — so a tampered/stale client total can't skew it."""
    await applicant(applicant_id, user, session)
    if not payload.criteria:
        raise HTTPException(status_code=422, detail="At least one rubric criterion is required")
    for criterion in payload.criteria:
        if criterion.weight < 0:
            raise HTTPException(status_code=422, detail="Criterion weight cannot be negative")
        if criterion.score < 0:
            raise HTTPException(status_code=422, detail="Criterion score cannot be negative")
    if payload.recommendation and payload.recommendation not in ("admit", "waitlist", "reject"):
        raise HTTPException(status_code=422, detail="Invalid recommendation")

    total_weight = sum(c.weight for c in payload.criteria)
    total_score = (
        sum(c.weight * c.score for c in payload.criteria) / total_weight
        if total_weight > 0
        else sum(c.score for c in payload.criteria) / len(payload.criteria)
    )

    item = ApplicationReview(
        school_id=scope(user),
        applicant_id=applicant_id,
        reviewer_id=user.id,
        criteria=json.dumps([c.model_dump() for c in payload.criteria]),
        total_score=round(total_score, 2),
        recommendation=payload.recommendation,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _review_dict(item)


@router.get("/applicants/{applicant_id}/reviews", response_model=list[dict])
async def list_application_reviews(applicant_id: str, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    await applicant(applicant_id, user, session)
    result = await session.execute(
        select(ApplicationReview).where(ApplicationReview.applicant_id == applicant_id, ApplicationReview.school_id == scope(user)).order_by(ApplicationReview.created_at.desc())
    )
    return [_review_dict(item) for item in result.scalars().all()]