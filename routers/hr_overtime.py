"""Staff overtime claims: record, approve/reject, and push approved hours
into payroll as a PayrollAdjustment (the actual payment math still runs
through payroll's existing adjustment/approval flow — this module only
owns the claim and its approval, not the money)."""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user, require_permission
from database import get_session
from models.overtime import OvertimeRecord, OvertimeRecordCreate, OvertimeRecordReject
from models.payroll import PayrollContract
from models.staff import Staff
from models.user import User, UserRole
from services.payroll_service import PayrollService

router = APIRouter(prefix="/hr/overtime", tags=["HR Overtime"])
WRITE_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR)


def _school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


async def _own_staff_id(user: User, session: AsyncSession) -> str | None:
    result = await session.execute(select(Staff.id).where(Staff.user_id == user.id))
    return result.scalar_one_or_none()


@router.get("", response_model=list[dict])
async def list_overtime(staff_id: str | None = None, status: str | None = None, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    query = select(OvertimeRecord).where(OvertimeRecord.school_id == _school_id(current_user))
    if staff_id:
        query = query.where(OvertimeRecord.staff_id == staff_id)
    elif current_user.role not in WRITE_ROLES:
        own_id = await _own_staff_id(current_user, session)
        if not own_id:
            return []
        query = query.where(OvertimeRecord.staff_id == own_id)
    if status:
        query = query.where(OvertimeRecord.status == status)
    result = await session.execute(query.order_by(OvertimeRecord.work_date.desc()))
    return [item.model_dump() for item in result.scalars().all()]


@router.post("", response_model=dict)
async def create_overtime(payload: OvertimeRecordCreate, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """A staff member can claim their own overtime; HR/admin can log it for
    anyone."""
    own_id = await _own_staff_id(current_user, session)
    if current_user.role not in WRITE_ROLES and payload.staff_id != own_id:
        raise HTTPException(status_code=403, detail="You can only claim overtime for yourself")
    staff = (await session.execute(select(Staff).where(Staff.id == payload.staff_id, Staff.school_id == _school_id(current_user)))).scalar_one_or_none()
    if not staff:
        raise HTTPException(status_code=400, detail="Staff member not found in this school")
    if payload.hours <= 0 or payload.hours > 24:
        raise HTTPException(status_code=422, detail="hours must be between 0 and 24")
    item = OvertimeRecord(school_id=_school_id(current_user), recorded_by=current_user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.post("/{overtime_id}/approve", response_model=dict)
async def approve_overtime(overtime_id: str, current_user: User = Depends(require_permission("hr.overtime.approve")), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(OvertimeRecord).where(OvertimeRecord.id == overtime_id, OvertimeRecord.school_id == _school_id(current_user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Overtime record not found")
    if item.status != "pending":
        raise HTTPException(status_code=400, detail=f"Cannot approve a record in {item.status} status")
    own_id = await _own_staff_id(current_user, session)
    if own_id and own_id == item.staff_id:
        raise HTTPException(status_code=403, detail="You cannot approve your own overtime claim — ask another admin to approve it")
    item.status = "approved"
    item.approved_by = current_user.id
    item.approved_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.post("/{overtime_id}/reject", response_model=dict)
async def reject_overtime(overtime_id: str, payload: OvertimeRecordReject, current_user: User = Depends(require_permission("hr.overtime.approve")), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(OvertimeRecord).where(OvertimeRecord.id == overtime_id, OvertimeRecord.school_id == _school_id(current_user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Overtime record not found")
    if item.status != "pending":
        raise HTTPException(status_code=400, detail=f"Cannot reject a record in {item.status} status")
    item.status = "rejected"
    item.approved_by = current_user.id
    item.approved_at = datetime.utcnow()
    item.rejection_reason = payload.rejection_reason
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


async def _push_one_to_payroll(item: OvertimeRecord, payroll_run_id: str, school_id: str, created_by: str, session: AsyncSession) -> dict:
    """Shared core of the single and bulk push endpoints below — turns one
    approved overtime claim into a pending PayrollAdjustment on the given
    run, so it flows through payroll's own approval/math rather than this
    module paying anything directly. Raises HTTPException on failure so the
    single-record endpoint's existing behavior is unchanged; the bulk
    endpoint catches it per-record instead."""
    if item.status != "approved":
        raise HTTPException(status_code=400, detail="Only approved overtime can be pushed to payroll")
    if item.payroll_adjustment_id:
        raise HTTPException(status_code=400, detail="This overtime record has already been pushed to payroll")

    contract = (await session.execute(
        select(PayrollContract).where(PayrollContract.staff_id == item.staff_id, PayrollContract.is_active == True)
        .order_by(PayrollContract.effective_from.desc())
    )).scalars().first()
    hourly_rate = (contract.basic_salary / (contract.standard_monthly_hours or 160)) if contract and contract.basic_salary else 0
    amount = round(item.hours * item.rate_multiplier * hourly_rate, 2)

    service = PayrollService(session)
    adj_result = await service.create_adjustment(
        school_id=school_id, payroll_run_id=payroll_run_id, staff_id=item.staff_id,
        adjustment_type="overtime", amount=amount,
        reason=f"Overtime: {item.hours}h @ {item.rate_multiplier}x on {item.work_date}" + (f" — {item.reason}" if item.reason else ""),
        created_by=created_by,
    )
    if not adj_result.get("success"):
        raise HTTPException(status_code=400, detail=adj_result.get("error"))

    item.status = "paid"
    item.payroll_adjustment_id = adj_result.get("adjustment_id")
    session.add(item)
    return item.model_dump()


@router.post("/{overtime_id}/push-to-payroll", response_model=dict)
async def push_overtime_to_payroll(overtime_id: str, payroll_run_id: str, current_user: User = Depends(require_permission("hr.overtime.post")), session: AsyncSession = Depends(get_session)):
    """Turns an approved overtime claim into a pending PayrollAdjustment on
    the given run, so it flows through payroll's own approval/math rather
    than this module paying anything directly."""
    result = await session.execute(select(OvertimeRecord).where(OvertimeRecord.id == overtime_id, OvertimeRecord.school_id == _school_id(current_user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Overtime record not found")

    await _push_one_to_payroll(item, payroll_run_id, _school_id(current_user), current_user.id, session)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.post("/push-to-payroll/bulk", response_model=dict)
async def bulk_push_overtime_to_payroll(payroll_run_id: str, current_user: User = Depends(require_permission("hr.overtime.post")), session: AsyncSession = Depends(get_session)):
    """Push EVERY approved-but-not-yet-pushed overtime record for this
    school onto the given run in one call — previously overtime could only
    ever be pushed one claim at a time, so a school with many approved
    claims for a period had to click through each one individually."""
    school_id = _school_id(current_user)
    result = await session.execute(
        select(OvertimeRecord).where(
            OvertimeRecord.school_id == school_id,
            OvertimeRecord.status == "approved",
            OvertimeRecord.payroll_adjustment_id.is_(None),
        )
    )
    records = result.scalars().all()

    pushed = 0
    failed = []
    for item in records:
        try:
            await _push_one_to_payroll(item, payroll_run_id, school_id, current_user.id, session)
            pushed += 1
        except HTTPException as e:
            failed.append({"overtime_id": item.id, "staff_id": item.staff_id, "error": e.detail})

    await session.commit()
    return {"pushed": pushed, "failed_count": len(failed), "failed": failed, "total_candidates": len(records)}
