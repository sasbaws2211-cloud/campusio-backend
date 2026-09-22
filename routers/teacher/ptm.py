"""Teacher Portal - Parent-Teacher Meeting scheduling.

Teachers publish open time slots; parents book their children into them.
See services/ptm_service.py for the slot/booking lifecycle.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from typing import Optional

from models.ptm import PTMSlotCreate, PTMCancelRequest
from models.staff import Staff
from models.student import Parent
from models.user import User, UserRole
from database import get_session
from auth import require_roles
from services.ptm_service import PTMService, PTMServiceError
from services.email_service import EmailService

router = APIRouter(prefix="/teacher/ptm", tags=["teacher-ptm"])


async def _resolve_teacher_staff(session: AsyncSession, current_user: User) -> Staff:
    result = await session.execute(select(Staff).where(Staff.user_id == current_user.id))
    staff = result.scalar_one_or_none()
    if not staff:
        result = await session.execute(
            select(Staff).where(Staff.email == current_user.email, Staff.school_id == current_user.school_id)
        )
        staff = result.scalar_one_or_none()
    if not staff:
        raise HTTPException(status_code=403, detail="Staff profile not found")
    return staff


def _serialize_slot(slot) -> dict:
    return {
        "id": slot.id,
        "date": slot.date,
        "start_time": slot.start_time,
        "end_time": slot.end_time,
        "location": slot.location,
        "status": slot.status,
    }


@router.post("/slots", response_model=dict)
async def create_slot(
    body: PTMSlotCreate,
    current_user: User = Depends(require_roles(UserRole.TEACHER)),
    session: AsyncSession = Depends(get_session),
):
    staff = await _resolve_teacher_staff(session, current_user)
    service = PTMService(session)
    try:
        slot = await service.create_slot(
            current_user.school_id, staff.id, body.date, body.start_time, body.end_time, body.location,
        )
    except PTMServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize_slot(slot)


@router.get("/slots", response_model=dict)
async def list_my_slots(
    from_date: Optional[str] = None,
    current_user: User = Depends(require_roles(UserRole.TEACHER)),
    session: AsyncSession = Depends(get_session),
):
    staff = await _resolve_teacher_staff(session, current_user)
    service = PTMService(session)
    slots = await service.list_teacher_slots(current_user.school_id, staff.id, from_date)
    return {"slots": [_serialize_slot(s) for s in slots]}


@router.delete("/slots/{slot_id}", response_model=dict)
async def cancel_slot(
    slot_id: str,
    current_user: User = Depends(require_roles(UserRole.TEACHER)),
    session: AsyncSession = Depends(get_session),
):
    staff = await _resolve_teacher_staff(session, current_user)
    service = PTMService(session)
    try:
        await service.cancel_slot(current_user.school_id, staff.id, slot_id)
    except PTMServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "message": "Slot cancelled"}


@router.get("/bookings", response_model=dict)
async def list_my_bookings(
    current_user: User = Depends(require_roles(UserRole.TEACHER)),
    session: AsyncSession = Depends(get_session),
):
    staff = await _resolve_teacher_staff(session, current_user)
    service = PTMService(session)
    bookings = await service.list_teacher_bookings(current_user.school_id, staff.id)
    return {"bookings": bookings}


@router.post("/bookings/{booking_id}/cancel", response_model=dict)
async def cancel_booking(
    booking_id: str,
    body: PTMCancelRequest,
    current_user: User = Depends(require_roles(UserRole.TEACHER)),
    session: AsyncSession = Depends(get_session),
):
    staff = await _resolve_teacher_staff(session, current_user)
    service = PTMService(session)
    try:
        booking = await service.cancel_booking(
            current_user.school_id, booking_id, cancelled_by="teacher", reason=body.reason, teacher_id=staff.id,
        )
    except PTMServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))

    parent_result = await session.execute(select(Parent).where(Parent.id == booking.parent_id))
    parent = parent_result.scalar_one_or_none()
    if parent and parent.email:
        await EmailService().send_email(
            to=[parent.email],
            subject="Parent-teacher meeting cancelled",
            html_body=f"<p>Your meeting with {staff.first_name} {staff.last_name} has been cancelled by the teacher.</p>"
                      + (f"<p>Reason: {body.reason}</p>" if body.reason else ""),
        )

    return {"success": True, "message": "Booking cancelled"}
