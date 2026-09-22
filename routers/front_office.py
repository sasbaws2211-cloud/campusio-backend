"""Front Office / Visitor Management Router"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from sqlmodel import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
from typing import Optional, List
import uuid

from models.front_office import (
    FrontOfficeVisitor, FrontOfficeVisitorCreate, FrontOfficeVisitorUpdate, VisitorStatus,
    VisitorApprovalStatus, RejectVisitorRequest,
    GatePass, GatePassCreate, GatePassUpdate,
    Appointment, AppointmentCreate, AppointmentUpdate,
    CourierItem, CourierItemCreate, CourierItemUpdate,
)
from models.school import School
from models.student import Student
from models.staff import Staff
from models.user import User, UserRole
from database import get_session
from auth import get_current_user, require_roles

router = APIRouter(prefix="/front-office", tags=["Front Office"])

WRITE_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.SECURITY_OFFICER)


def _generate_badge_number() -> str:
    return f"V-{datetime.utcnow().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"


def _generate_pass_number() -> str:
    return f"GP-{datetime.utcnow().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"


async def _requires_maker_checker(session: AsyncSession, school_id: str) -> bool:
    """Whether this school has segregation-of-duties enabled

    Off by default (School.require_maker_checker) — mirrors
    services/journal_entry_service.py::requires_maker_checker exactly.
    """
    result = await session.execute(
        select(School.require_maker_checker).where(School.id == school_id)
    )
    return bool(result.scalar_one_or_none())


@router.get("/visitors", response_model=List[dict])
async def list_visitors(
    status: Optional[VisitorStatus] = None,
    approval_status: Optional[VisitorApprovalStatus] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """List front-office visitor records, optionally filtered by status"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(FrontOfficeVisitor).where(FrontOfficeVisitor.school_id == school_id)
    if status:
        query = query.where(FrontOfficeVisitor.status == status)
    if approval_status:
        query = query.where(FrontOfficeVisitor.approval_status == approval_status)
    query = query.order_by(FrontOfficeVisitor.check_in_time.desc()).offset(skip).limit(limit)

    result = await session.execute(query)
    records = result.scalars().all()
    return [jsonable_encoder(r) for r in records]


@router.post("/visitors", response_model=dict)
async def check_in_visitor(
    visitor_data: FrontOfficeVisitorCreate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Check in a new visitor, issuing a badge number. If the school has
    maker-checker enabled (School.require_maker_checker), the record is
    logged PENDING and needs sign-off from a different staff member via
    POST /visitors/{id}/approve — this is a pure audit confirmation and does
    NOT delay or block the check-in itself, which always happens
    immediately regardless of this setting. Off by default, in which case
    it's auto-approved immediately — same as this module's original
    behavior."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    maker_checker = await _requires_maker_checker(session, school_id)
    visitor = FrontOfficeVisitor(
        **visitor_data.dict(),
        school_id=school_id,
        badge_number=_generate_badge_number(),
        status=VisitorStatus.CHECKED_IN,
        created_by=current_user.id,
        approval_status=VisitorApprovalStatus.PENDING if maker_checker else VisitorApprovalStatus.APPROVED,
        approved_by=None if maker_checker else current_user.id,
        approved_at=None if maker_checker else datetime.utcnow(),
    )
    session.add(visitor)
    await session.commit()
    await session.refresh(visitor)

    return jsonable_encoder(visitor)


@router.post("/visitors/{visitor_id}/approve", response_model=dict)
async def approve_visitor(
    visitor_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(FrontOfficeVisitor).where(and_(FrontOfficeVisitor.id == visitor_id, FrontOfficeVisitor.school_id == school_id))
    )
    visitor = result.scalar_one_or_none()
    if not visitor:
        raise HTTPException(status_code=404, detail="Visitor not found")
    if visitor.approval_status != VisitorApprovalStatus.PENDING:
        raise HTTPException(status_code=400, detail=f"Cannot approve a visitor record with status {visitor.approval_status.value}")

    if visitor.created_by == current_user.id and await _requires_maker_checker(session, school_id):
        raise HTTPException(status_code=403, detail="Segregation of duties: you checked in this visitor and cannot also approve it")

    visitor.approval_status = VisitorApprovalStatus.APPROVED
    visitor.approved_by = current_user.id
    visitor.approved_at = datetime.utcnow()
    session.add(visitor)
    await session.commit()
    await session.refresh(visitor)
    return jsonable_encoder(visitor)


@router.post("/visitors/{visitor_id}/reject", response_model=dict)
async def reject_visitor(
    visitor_id: str,
    data: RejectVisitorRequest,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(FrontOfficeVisitor).where(and_(FrontOfficeVisitor.id == visitor_id, FrontOfficeVisitor.school_id == school_id))
    )
    visitor = result.scalar_one_or_none()
    if not visitor:
        raise HTTPException(status_code=404, detail="Visitor not found")
    if visitor.approval_status != VisitorApprovalStatus.PENDING:
        raise HTTPException(status_code=400, detail=f"Cannot reject a visitor record with status {visitor.approval_status.value}")

    if visitor.created_by == current_user.id and await _requires_maker_checker(session, school_id):
        raise HTTPException(status_code=403, detail="Segregation of duties: you checked in this visitor and cannot also reject it")

    visitor.approval_status = VisitorApprovalStatus.REJECTED
    visitor.approved_by = current_user.id
    visitor.approved_at = datetime.utcnow()
    visitor.rejection_reason = data.rejection_reason
    session.add(visitor)
    await session.commit()
    await session.refresh(visitor)
    return jsonable_encoder(visitor)


@router.get("/visitors/by-badge/{badge_number}", response_model=dict)
async def get_visitor_by_badge(
    badge_number: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Look up a visitor by badge number (used by the scan-to-checkout flow)"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(FrontOfficeVisitor).where(
            and_(
                FrontOfficeVisitor.badge_number == badge_number,
                FrontOfficeVisitor.school_id == school_id
            )
        )
    )
    visitor = result.scalar_one_or_none()
    if not visitor:
        raise HTTPException(status_code=404, detail="Visitor not found")

    return jsonable_encoder(visitor)


@router.get("/visitors/{visitor_id}", response_model=dict)
async def get_visitor(
    visitor_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get a single visitor record"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(FrontOfficeVisitor).where(
            and_(FrontOfficeVisitor.id == visitor_id, FrontOfficeVisitor.school_id == school_id)
        )
    )
    visitor = result.scalar_one_or_none()
    if not visitor:
        raise HTTPException(status_code=404, detail="Visitor not found")

    return jsonable_encoder(visitor)


@router.put("/visitors/{visitor_id}", response_model=dict)
async def update_visitor(
    visitor_id: str,
    visitor_data: FrontOfficeVisitorUpdate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Edit a visitor record while checked in"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(FrontOfficeVisitor).where(
            and_(FrontOfficeVisitor.id == visitor_id, FrontOfficeVisitor.school_id == school_id)
        )
    )
    visitor = result.scalar_one_or_none()
    if not visitor:
        raise HTTPException(status_code=404, detail="Visitor not found")

    update_data = visitor_data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(visitor, key, value)

    session.add(visitor)
    await session.commit()
    await session.refresh(visitor)

    return jsonable_encoder(visitor)


@router.post("/visitors/{visitor_id}/check-out", response_model=dict)
async def check_out_visitor(
    visitor_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Check out a visitor"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(FrontOfficeVisitor).where(
            and_(FrontOfficeVisitor.id == visitor_id, FrontOfficeVisitor.school_id == school_id)
        )
    )
    visitor = result.scalar_one_or_none()
    if not visitor:
        raise HTTPException(status_code=404, detail="Visitor not found")

    if visitor.status == VisitorStatus.CHECKED_OUT:
        raise HTTPException(status_code=409, detail="Visitor already checked out")

    visitor.check_out_time = datetime.utcnow()
    visitor.status = VisitorStatus.CHECKED_OUT

    session.add(visitor)
    await session.commit()
    await session.refresh(visitor)

    return jsonable_encoder(visitor)


@router.delete("/visitors/{visitor_id}", response_model=dict)
async def delete_visitor(
    visitor_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Delete a visitor record"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(FrontOfficeVisitor).where(
            and_(FrontOfficeVisitor.id == visitor_id, FrontOfficeVisitor.school_id == school_id)
        )
    )
    visitor = result.scalar_one_or_none()
    if not visitor:
        raise HTTPException(status_code=404, detail="Visitor not found")

    await session.delete(visitor)
    await session.commit()

    return {"message": "Visitor record deleted successfully", "id": visitor_id}


# ---------------------------------------------------------------------------
# Gate Passes
# ---------------------------------------------------------------------------

@router.post("/gate-passes", response_model=dict)
async def create_gate_pass(
    data: GatePassCreate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Issue a gate pass for a student or staff member leaving campus. If
    the school has maker-checker enabled, the pass starts PENDING and needs
    sign-off from a different staff member via POST /gate-passes/{id}/approve
    before the holder can exit. Off by default, in which case it's
    auto-approved immediately — mirrors check_in_visitor's branching."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    if data.person_type not in ("student", "staff"):
        raise HTTPException(status_code=400, detail="person_type must be 'student' or 'staff'")

    if data.person_type == "student":
        result = await session.execute(
            select(Student.id).where(and_(Student.id == data.person_id, Student.school_id == school_id))
        )
    else:
        result = await session.execute(
            select(Staff.id).where(and_(Staff.id == data.person_id, Staff.school_id == school_id))
        )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail=f"{data.person_type.capitalize()} not found")

    maker_checker = await _requires_maker_checker(session, school_id)
    gate_pass = GatePass(
        **data.dict(),
        school_id=school_id,
        pass_number=_generate_pass_number(),
        status="pending" if maker_checker else "approved",
        approver_id=None if maker_checker else current_user.id,
        created_by=current_user.id,
    )
    session.add(gate_pass)
    await session.commit()
    await session.refresh(gate_pass)

    return jsonable_encoder(gate_pass)


@router.get("/gate-passes", response_model=List[dict])
async def list_gate_passes(
    status: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """List gate passes, optionally filtered by status"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(GatePass).where(GatePass.school_id == school_id)
    if status:
        query = query.where(GatePass.status == status)
    query = query.order_by(GatePass.created_at.desc()).offset(skip).limit(limit)

    result = await session.execute(query)
    records = result.scalars().all()
    return [jsonable_encoder(r) for r in records]


@router.post("/gate-passes/{gate_pass_id}/approve", response_model=dict)
async def approve_gate_pass(
    gate_pass_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(GatePass).where(and_(GatePass.id == gate_pass_id, GatePass.school_id == school_id))
    )
    gate_pass = result.scalar_one_or_none()
    if not gate_pass:
        raise HTTPException(status_code=404, detail="Gate pass not found")
    if gate_pass.status != "pending":
        raise HTTPException(status_code=400, detail=f"Cannot approve a gate pass with status {gate_pass.status}")

    if gate_pass.created_by == current_user.id and await _requires_maker_checker(session, school_id):
        raise HTTPException(status_code=403, detail="Segregation of duties: you created this gate pass and cannot also approve it")

    gate_pass.status = "approved"
    gate_pass.approver_id = current_user.id
    gate_pass.updated_at = datetime.utcnow()
    session.add(gate_pass)
    await session.commit()
    await session.refresh(gate_pass)
    return jsonable_encoder(gate_pass)


@router.post("/gate-passes/{gate_pass_id}/reject", response_model=dict)
async def reject_gate_pass(
    gate_pass_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(GatePass).where(and_(GatePass.id == gate_pass_id, GatePass.school_id == school_id))
    )
    gate_pass = result.scalar_one_or_none()
    if not gate_pass:
        raise HTTPException(status_code=404, detail="Gate pass not found")
    if gate_pass.status != "pending":
        raise HTTPException(status_code=400, detail=f"Cannot reject a gate pass with status {gate_pass.status}")

    if gate_pass.created_by == current_user.id and await _requires_maker_checker(session, school_id):
        raise HTTPException(status_code=403, detail="Segregation of duties: you created this gate pass and cannot also reject it")

    gate_pass.status = "rejected"
    gate_pass.approver_id = current_user.id
    gate_pass.updated_at = datetime.utcnow()
    session.add(gate_pass)
    await session.commit()
    await session.refresh(gate_pass)
    return jsonable_encoder(gate_pass)


@router.post("/gate-passes/{gate_pass_id}/exit", response_model=dict)
async def exit_gate_pass(
    gate_pass_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Record the holder physically leaving campus. Only valid from
    'approved' status."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(GatePass).where(and_(GatePass.id == gate_pass_id, GatePass.school_id == school_id))
    )
    gate_pass = result.scalar_one_or_none()
    if not gate_pass:
        raise HTTPException(status_code=404, detail="Gate pass not found")
    if gate_pass.status != "approved":
        raise HTTPException(status_code=400, detail=f"Cannot record exit for a gate pass with status {gate_pass.status}")

    gate_pass.status = "out"
    gate_pass.exit_time = datetime.utcnow()
    gate_pass.updated_at = datetime.utcnow()
    session.add(gate_pass)
    await session.commit()
    await session.refresh(gate_pass)
    return jsonable_encoder(gate_pass)


@router.post("/gate-passes/{gate_pass_id}/return", response_model=dict)
async def return_gate_pass(
    gate_pass_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Record the holder returning to campus. Only valid from 'out' status."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(GatePass).where(and_(GatePass.id == gate_pass_id, GatePass.school_id == school_id))
    )
    gate_pass = result.scalar_one_or_none()
    if not gate_pass:
        raise HTTPException(status_code=404, detail="Gate pass not found")
    if gate_pass.status != "out":
        raise HTTPException(status_code=400, detail=f"Cannot record return for a gate pass with status {gate_pass.status}")

    gate_pass.status = "returned"
    gate_pass.actual_return_time = datetime.utcnow()
    gate_pass.updated_at = datetime.utcnow()
    session.add(gate_pass)
    await session.commit()
    await session.refresh(gate_pass)
    return jsonable_encoder(gate_pass)


@router.get("/gate-passes/by-number/{pass_number}", response_model=dict)
async def get_gate_pass_by_number(
    pass_number: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Look up a gate pass by pass number (used at an exit gate)"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(GatePass).where(
            and_(GatePass.pass_number == pass_number, GatePass.school_id == school_id)
        )
    )
    gate_pass = result.scalar_one_or_none()
    if not gate_pass:
        raise HTTPException(status_code=404, detail="Gate pass not found")

    return jsonable_encoder(gate_pass)


# ---------------------------------------------------------------------------
# Appointments
# ---------------------------------------------------------------------------

@router.post("/appointments", response_model=dict)
async def create_appointment(
    data: AppointmentCreate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    appointment = Appointment(
        **data.dict(),
        school_id=school_id,
        status="requested",
        created_by=current_user.id,
    )
    session.add(appointment)
    await session.commit()
    await session.refresh(appointment)
    return jsonable_encoder(appointment)


@router.get("/appointments", response_model=List[dict])
async def list_appointments(
    status: Optional[str] = None,
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """List appointments, optionally filtered by status and/or a
    requested_datetime range"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(Appointment).where(Appointment.school_id == school_id)
    if status:
        query = query.where(Appointment.status == status)
    if from_date:
        query = query.where(Appointment.requested_datetime >= from_date)
    if to_date:
        query = query.where(Appointment.requested_datetime <= to_date)
    query = query.order_by(Appointment.requested_datetime.asc()).offset(skip).limit(limit)

    result = await session.execute(query)
    records = result.scalars().all()
    return [jsonable_encoder(r) for r in records]


@router.put("/appointments/{appointment_id}", response_model=dict)
async def update_appointment(
    appointment_id: str,
    data: AppointmentUpdate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Edit an appointment — e.g. staff confirms (status='confirmed') or
    cancels (status='cancelled')"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(Appointment).where(and_(Appointment.id == appointment_id, Appointment.school_id == school_id))
    )
    appointment = result.scalar_one_or_none()
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")

    update_data = data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(appointment, key, value)
    appointment.updated_at = datetime.utcnow()

    session.add(appointment)
    await session.commit()
    await session.refresh(appointment)
    return jsonable_encoder(appointment)


@router.post("/appointments/{appointment_id}/check-in", response_model=dict)
async def check_in_appointment(
    appointment_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """The bridge into the walk-in flow: when the appointment's visitor
    arrives, this creates a real FrontOfficeVisitor check-in record (reusing
    the same badge-number generation and maker-checker branching as
    check_in_visitor) and marks the appointment completed."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(Appointment).where(and_(Appointment.id == appointment_id, Appointment.school_id == school_id))
    )
    appointment = result.scalar_one_or_none()
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")
    if appointment.status not in ("requested", "confirmed"):
        raise HTTPException(status_code=400, detail=f"Cannot check in an appointment with status {appointment.status}")

    maker_checker = await _requires_maker_checker(session, school_id)
    visitor = FrontOfficeVisitor(
        school_id=school_id,
        visitor_name=appointment.visitor_name,
        visitor_phone=appointment.visitor_phone,
        purpose=appointment.purpose,
        host_staff_id=appointment.staff_to_meet_id,
        badge_number=_generate_badge_number(),
        status=VisitorStatus.CHECKED_IN,
        created_by=current_user.id,
        approval_status=VisitorApprovalStatus.PENDING if maker_checker else VisitorApprovalStatus.APPROVED,
        approved_by=None if maker_checker else current_user.id,
        approved_at=None if maker_checker else datetime.utcnow(),
    )
    session.add(visitor)

    appointment.status = "completed"
    appointment.checked_in_visitor_id = visitor.id
    appointment.updated_at = datetime.utcnow()
    session.add(appointment)

    await session.commit()
    await session.refresh(visitor)
    await session.refresh(appointment)

    return {"visitor": jsonable_encoder(visitor), "appointment": jsonable_encoder(appointment)}


# ---------------------------------------------------------------------------
# Courier / Mail Tracking
# ---------------------------------------------------------------------------

@router.post("/courier-items", response_model=dict)
async def create_courier_item(
    data: CourierItemCreate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    if data.direction not in ("inbound", "outbound"):
        raise HTTPException(status_code=400, detail="direction must be 'inbound' or 'outbound'")

    now = datetime.utcnow()
    courier_item = CourierItem(
        **data.dict(),
        school_id=school_id,
        status="received" if data.direction == "inbound" else "dispatched",
        received_at=now if data.direction == "inbound" else None,
        dispatched_at=now if data.direction == "outbound" else None,
        created_by=current_user.id,
    )
    session.add(courier_item)
    await session.commit()
    await session.refresh(courier_item)
    return jsonable_encoder(courier_item)


@router.get("/courier-items", response_model=List[dict])
async def list_courier_items(
    direction: Optional[str] = None,
    status: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """List courier/mail items, optionally filtered by direction/status"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(CourierItem).where(CourierItem.school_id == school_id)
    if direction:
        query = query.where(CourierItem.direction == direction)
    if status:
        query = query.where(CourierItem.status == status)
    query = query.order_by(CourierItem.created_at.desc()).offset(skip).limit(limit)

    result = await session.execute(query)
    records = result.scalars().all()
    return [jsonable_encoder(r) for r in records]


@router.get("/courier-items/{courier_item_id}", response_model=dict)
async def get_courier_item(
    courier_item_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(CourierItem).where(and_(CourierItem.id == courier_item_id, CourierItem.school_id == school_id))
    )
    courier_item = result.scalar_one_or_none()
    if not courier_item:
        raise HTTPException(status_code=404, detail="Courier item not found")

    return jsonable_encoder(courier_item)


@router.put("/courier-items/{courier_item_id}", response_model=dict)
async def update_courier_item(
    courier_item_id: str,
    data: CourierItemUpdate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Edit a courier item — e.g. status transitions such as
    'notified' -> 'collected' when the recipient picks it up"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(CourierItem).where(and_(CourierItem.id == courier_item_id, CourierItem.school_id == school_id))
    )
    courier_item = result.scalar_one_or_none()
    if not courier_item:
        raise HTTPException(status_code=404, detail="Courier item not found")

    update_data = data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(courier_item, key, value)

    session.add(courier_item)
    await session.commit()
    await session.refresh(courier_item)
    return jsonable_encoder(courier_item)
