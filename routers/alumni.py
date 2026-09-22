"""Alumni Management Router"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from sqlmodel import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
from typing import Optional, List

from models.alumni import (
    AlumniRecord, AlumniRecordCreate, AlumniRecordUpdate, AlumniOutreachRequest,
    AlumniDonation, AlumniDonationCreate, DonationApprovalStatus, RejectDonationRequest,
)
from models.school import School
from models.user import User, UserRole
from database import get_session
from auth import get_current_user, require_roles
from services.sms_service import sms_service
from services.email_service import email_service

router = APIRouter(prefix="/alumni", tags=["Alumni"])

WRITE_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)


async def _requires_maker_checker(session: AsyncSession, school_id: str) -> bool:
    """Whether this school has segregation-of-duties enabled

    Off by default (School.require_maker_checker) — mirrors
    services/journal_entry_service.py::requires_maker_checker exactly.
    """
    result = await session.execute(
        select(School.require_maker_checker).where(School.id == school_id)
    )
    return bool(result.scalar_one_or_none())


@router.get("/records", response_model=List[dict])
async def list_alumni_records(
    graduation_year: Optional[str] = None,
    opt_in_communications: Optional[bool] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(AlumniRecord).where(AlumniRecord.school_id == school_id)
    if graduation_year:
        query = query.where(AlumniRecord.graduation_year == graduation_year)
    if opt_in_communications is not None:
        query = query.where(AlumniRecord.opt_in_communications == opt_in_communications)
    query = query.order_by(AlumniRecord.graduation_year.desc()).offset(skip).limit(limit)

    result = await session.execute(query)
    return [jsonable_encoder(a) for a in result.scalars().all()]


@router.post("/records", response_model=dict)
async def create_alumni_record(
    data: AlumniRecordCreate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    record = AlumniRecord(**data.dict(), school_id=school_id)
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return jsonable_encoder(record)


@router.get("/records/{record_id}", response_model=dict)
async def get_alumni_record(
    record_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(AlumniRecord).where(and_(AlumniRecord.id == record_id, AlumniRecord.school_id == school_id))
    )
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Alumni record not found")
    return jsonable_encoder(record)


@router.put("/records/{record_id}", response_model=dict)
async def update_alumni_record(
    record_id: str,
    data: AlumniRecordUpdate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(AlumniRecord).where(and_(AlumniRecord.id == record_id, AlumniRecord.school_id == school_id))
    )
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Alumni record not found")

    update_data = data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(record, key, value)
    record.updated_at = datetime.utcnow()

    session.add(record)
    await session.commit()
    await session.refresh(record)
    return jsonable_encoder(record)


@router.delete("/records/{record_id}", response_model=dict)
async def delete_alumni_record(
    record_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(AlumniRecord).where(and_(AlumniRecord.id == record_id, AlumniRecord.school_id == school_id))
    )
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Alumni record not found")

    await session.delete(record)
    await session.commit()
    return {"message": "Alumni record deleted successfully", "id": record_id}


@router.post("/outreach", response_model=dict)
async def send_alumni_outreach(
    data: AlumniOutreachRequest,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Fan a message out to opted-in alumni via SMS (if phone) and email (if email)"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(AlumniRecord).where(
        and_(AlumniRecord.school_id == school_id, AlumniRecord.opt_in_communications == True)  # noqa: E712
    )
    if data.graduation_year:
        query = query.where(AlumniRecord.graduation_year == data.graduation_year)

    result = await session.execute(query)
    alumni = result.scalars().all()

    phone_numbers = [a.phone for a in alumni if a.phone]
    emails = [a.email for a in alumni if a.email]

    sms_result = None
    email_result = None
    if phone_numbers:
        sms_result = await sms_service.send_sms(phone_numbers, data.message)
    if emails:
        email_result = await email_service.send_email(
            to=emails,
            subject=data.subject or "Message from your school",
            html_body=f"<p>{data.message}</p>",
            text_body=data.message,
        )

    return {
        "recipients_matched": len(alumni),
        "sms_sent_to": len(phone_numbers),
        "emails_sent_to": len(emails),
        "sms_result": sms_result,
        "email_result": email_result,
    }


# ============================================================================
# DONATIONS / FUNDRAISING
# ============================================================================

@router.get("/donations", response_model=List[dict])
async def list_donations(
    alumni_id: Optional[str] = None,
    campaign: Optional[str] = None,
    approval_status: Optional[DonationApprovalStatus] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(AlumniDonation).where(AlumniDonation.school_id == school_id)
    if alumni_id:
        query = query.where(AlumniDonation.alumni_id == alumni_id)
    if campaign:
        query = query.where(AlumniDonation.campaign == campaign)
    if approval_status:
        query = query.where(AlumniDonation.approval_status == approval_status)
    query = query.order_by(AlumniDonation.donation_date.desc()).offset(skip).limit(limit)

    result = await session.execute(query)
    return [jsonable_encoder(d) for d in result.scalars().all()]


@router.post("/{alumni_id}/donations", response_model=dict)
async def create_donation(
    alumni_id: str,
    data: AlumniDonationCreate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Log a donation against an alumnus. If the school has maker-checker
    enabled (School.require_maker_checker), the donation is logged PENDING
    and doesn't count toward the fundraising totals in GET /donations/summary
    until a different staff member approves it via POST
    /donations/{id}/approve. Off by default, in which case it's
    auto-approved immediately — same as this module's original behavior."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    alumni_result = await session.execute(
        select(AlumniRecord).where(and_(AlumniRecord.id == alumni_id, AlumniRecord.school_id == school_id))
    )
    if not alumni_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Alumni record not found")

    maker_checker = await _requires_maker_checker(session, school_id)
    donation = AlumniDonation(
        **data.dict(),
        alumni_id=alumni_id,
        school_id=school_id,
        created_by=current_user.id,
        approval_status=DonationApprovalStatus.PENDING if maker_checker else DonationApprovalStatus.APPROVED,
        approved_by=None if maker_checker else current_user.id,
        approved_at=None if maker_checker else datetime.utcnow(),
    )
    session.add(donation)
    await session.commit()
    await session.refresh(donation)
    return jsonable_encoder(donation)


@router.post("/donations/{donation_id}/approve", response_model=dict)
async def approve_donation(
    donation_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(AlumniDonation).where(and_(AlumniDonation.id == donation_id, AlumniDonation.school_id == school_id))
    )
    donation = result.scalar_one_or_none()
    if not donation:
        raise HTTPException(status_code=404, detail="Donation not found")
    if donation.approval_status != DonationApprovalStatus.PENDING:
        raise HTTPException(status_code=400, detail=f"Cannot approve a donation with status {donation.approval_status.value}")

    if donation.created_by == current_user.id and await _requires_maker_checker(session, school_id):
        raise HTTPException(status_code=403, detail="Segregation of duties: you logged this donation and cannot also approve it")

    donation.approval_status = DonationApprovalStatus.APPROVED
    donation.approved_by = current_user.id
    donation.approved_at = datetime.utcnow()
    session.add(donation)
    await session.commit()
    await session.refresh(donation)
    return jsonable_encoder(donation)


@router.post("/donations/{donation_id}/reject", response_model=dict)
async def reject_donation(
    donation_id: str,
    data: RejectDonationRequest,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(AlumniDonation).where(and_(AlumniDonation.id == donation_id, AlumniDonation.school_id == school_id))
    )
    donation = result.scalar_one_or_none()
    if not donation:
        raise HTTPException(status_code=404, detail="Donation not found")
    if donation.approval_status != DonationApprovalStatus.PENDING:
        raise HTTPException(status_code=400, detail=f"Cannot reject a donation with status {donation.approval_status.value}")

    if donation.created_by == current_user.id and await _requires_maker_checker(session, school_id):
        raise HTTPException(status_code=403, detail="Segregation of duties: you logged this donation and cannot also reject it")

    donation.approval_status = DonationApprovalStatus.REJECTED
    donation.approved_by = current_user.id
    donation.approved_at = datetime.utcnow()
    donation.rejection_reason = data.rejection_reason
    session.add(donation)
    await session.commit()
    await session.refresh(donation)
    return jsonable_encoder(donation)


@router.delete("/donations/{donation_id}", response_model=dict)
async def delete_donation(
    donation_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(AlumniDonation).where(and_(AlumniDonation.id == donation_id, AlumniDonation.school_id == school_id))
    )
    donation = result.scalar_one_or_none()
    if not donation:
        raise HTTPException(status_code=404, detail="Donation not found")

    await session.delete(donation)
    await session.commit()
    return {"message": "Donation deleted successfully", "id": donation_id}


@router.get("/donations/summary", response_model=dict)
async def get_donations_summary(
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Total raised overall and broken down by campaign — counts only
    APPROVED donations (see AlumniDonation.approval_status docstring:
    pending/rejected entries don't affect fundraising totals until
    confirmed)."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    total_result = await session.execute(
        select(func.coalesce(func.sum(AlumniDonation.amount), 0)).where(
            AlumniDonation.school_id == school_id,
            AlumniDonation.approval_status == DonationApprovalStatus.APPROVED,
        )
    )
    total = total_result.scalar_one()

    by_campaign_result = await session.execute(
        select(
            func.coalesce(AlumniDonation.campaign, "Uncategorized"),
            func.coalesce(func.sum(AlumniDonation.amount), 0),
        )
        .where(
            AlumniDonation.school_id == school_id,
            AlumniDonation.approval_status == DonationApprovalStatus.APPROVED,
        )
        .group_by(AlumniDonation.campaign)
    )
    by_campaign = {row[0]: row[1] for row in by_campaign_result.all()}

    return {"total_raised": total, "by_campaign": by_campaign}
