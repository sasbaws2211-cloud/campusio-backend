"""Parent-teacher meeting scheduling — teacher publishes open slots, a
parent books their child into one. See models/ptm.py for the slot/booking
lifecycle.
"""
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from models.ptm import PTMSlot, PTMSlotStatus, PTMBooking, PTMBookingStatus
from models.student import StudentParent, Student, Parent
from models.staff import Staff

logger = logging.getLogger(__name__)


def _times_overlap(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    """'HH:MM' 24-hour strings compare lexicographically the same as
    chronologically — same trick used by exams.py's conflict checks."""
    return start_a < end_b and start_b < end_a


class PTMServiceError(Exception):
    pass


class PTMService:
    def __init__(self, session: AsyncSession):
        self.session = session

    # ---- Teacher: slots ----

    async def create_slot(
        self, school_id: str, teacher_id: str, date: str, start_time: str, end_time: str, location: Optional[str],
    ) -> PTMSlot:
        if start_time >= end_time:
            raise PTMServiceError("Start time must be before end time")

        existing_result = await self.session.execute(
            select(PTMSlot).where(
                PTMSlot.school_id == school_id,
                PTMSlot.teacher_id == teacher_id,
                PTMSlot.date == date,
                PTMSlot.status != PTMSlotStatus.CANCELLED,
            )
        )
        for other in existing_result.scalars().all():
            if _times_overlap(start_time, end_time, other.start_time, other.end_time):
                raise PTMServiceError(f"This overlaps a slot you already have on {date} ({other.start_time}-{other.end_time})")

        slot = PTMSlot(
            school_id=school_id, teacher_id=teacher_id, date=date,
            start_time=start_time, end_time=end_time, location=location,
        )
        self.session.add(slot)
        await self.session.commit()
        await self.session.refresh(slot)
        return slot

    async def list_teacher_slots(self, school_id: str, teacher_id: str, from_date: Optional[str] = None) -> List[PTMSlot]:
        query = select(PTMSlot).where(PTMSlot.school_id == school_id, PTMSlot.teacher_id == teacher_id)
        if from_date:
            query = query.where(PTMSlot.date >= from_date)
        query = query.order_by(PTMSlot.date, PTMSlot.start_time)
        result = await self.session.execute(query)
        return result.scalars().all()

    async def cancel_slot(self, school_id: str, teacher_id: str, slot_id: str) -> PTMSlot:
        result = await self.session.execute(
            select(PTMSlot).where(PTMSlot.id == slot_id, PTMSlot.school_id == school_id, PTMSlot.teacher_id == teacher_id)
        )
        slot = result.scalar_one_or_none()
        if not slot:
            raise PTMServiceError("Slot not found")
        if slot.status == PTMSlotStatus.BOOKED:
            raise PTMServiceError("This slot has a confirmed booking — cancel the booking first")
        slot.status = PTMSlotStatus.CANCELLED
        slot.updated_at = datetime.utcnow()
        self.session.add(slot)
        await self.session.commit()
        return slot

    # ---- Parent: browse + book ----

    async def list_open_slots(self, school_id: str, teacher_id: Optional[str], from_date: Optional[str] = None) -> List[PTMSlot]:
        query = select(PTMSlot).where(PTMSlot.school_id == school_id, PTMSlot.status == PTMSlotStatus.OPEN)
        if teacher_id:
            query = query.where(PTMSlot.teacher_id == teacher_id)
        if from_date:
            query = query.where(PTMSlot.date >= from_date)
        query = query.order_by(PTMSlot.date, PTMSlot.start_time)
        result = await self.session.execute(query)
        return result.scalars().all()

    async def _verify_child(self, school_id: str, parent_id: str, student_id: str) -> None:
        result = await self.session.execute(
            select(StudentParent).where(StudentParent.parent_id == parent_id, StudentParent.student_id == student_id)
        )
        if not result.scalar_one_or_none():
            raise PTMServiceError("This student is not linked to your parent account")

    async def book_slot(
        self, school_id: str, parent_id: str, slot_id: str, student_id: str, purpose: Optional[str],
    ) -> PTMBooking:
        await self._verify_child(school_id, parent_id, student_id)

        result = await self.session.execute(
            select(PTMSlot).where(PTMSlot.id == slot_id, PTMSlot.school_id == school_id).with_for_update()
        )
        slot = result.scalar_one_or_none()
        if not slot:
            raise PTMServiceError("Slot not found")
        if slot.status != PTMSlotStatus.OPEN:
            raise PTMServiceError("This slot is no longer available")

        slot.status = PTMSlotStatus.BOOKED
        slot.updated_at = datetime.utcnow()
        self.session.add(slot)

        booking = PTMBooking(
            school_id=school_id, slot_id=slot_id, teacher_id=slot.teacher_id,
            parent_id=parent_id, student_id=student_id, purpose=purpose,
        )
        self.session.add(booking)
        await self.session.commit()
        await self.session.refresh(booking)
        return booking

    async def list_parent_bookings(self, school_id: str, parent_id: str) -> List[Dict[str, Any]]:
        result = await self.session.execute(
            select(PTMBooking, PTMSlot)
            .join(PTMSlot, PTMBooking.slot_id == PTMSlot.id)
            .where(PTMBooking.school_id == school_id, PTMBooking.parent_id == parent_id)
            .order_by(PTMSlot.date, PTMSlot.start_time)
        )
        return await self._serialize_bookings(result.all())

    async def list_teacher_bookings(self, school_id: str, teacher_id: str) -> List[Dict[str, Any]]:
        result = await self.session.execute(
            select(PTMBooking, PTMSlot)
            .join(PTMSlot, PTMBooking.slot_id == PTMSlot.id)
            .where(PTMBooking.school_id == school_id, PTMBooking.teacher_id == teacher_id)
            .order_by(PTMSlot.date, PTMSlot.start_time)
        )
        return await self._serialize_bookings(result.all())

    async def _serialize_bookings(self, rows) -> List[Dict[str, Any]]:
        student_ids = {b.student_id for b, _ in rows}
        teacher_ids = {b.teacher_id for b, _ in rows}
        parent_ids = {b.parent_id for b, _ in rows}

        students_by_id, teachers_by_id, parents_by_id = {}, {}, {}
        if student_ids:
            r = await self.session.execute(select(Student).where(Student.id.in_(student_ids)))
            students_by_id = {s.id: f"{s.first_name} {s.last_name}" for s in r.scalars().all()}
        if teacher_ids:
            r = await self.session.execute(select(Staff).where(Staff.id.in_(teacher_ids)))
            teachers_by_id = {s.id: f"{s.first_name} {s.last_name}" for s in r.scalars().all()}
        if parent_ids:
            r = await self.session.execute(select(Parent).where(Parent.id.in_(parent_ids)))
            parents_by_id = {p.id: f"{p.first_name} {p.last_name}" for p in r.scalars().all()}

        return [
            {
                "booking_id": booking.id,
                "slot_id": slot.id,
                "date": slot.date,
                "start_time": slot.start_time,
                "end_time": slot.end_time,
                "location": slot.location,
                "teacher_id": booking.teacher_id,
                "teacher_name": teachers_by_id.get(booking.teacher_id, "Unknown"),
                "parent_id": booking.parent_id,
                "parent_name": parents_by_id.get(booking.parent_id, "Unknown"),
                "student_id": booking.student_id,
                "student_name": students_by_id.get(booking.student_id, "Unknown"),
                "purpose": booking.purpose,
                "status": booking.status,
                "cancelled_by": booking.cancelled_by,
                "cancellation_reason": booking.cancellation_reason,
            }
            for booking, slot in rows
        ]

    async def cancel_booking(
        self, school_id: str, booking_id: str, cancelled_by: str, reason: Optional[str],
        *, parent_id: Optional[str] = None, teacher_id: Optional[str] = None,
    ) -> PTMBooking:
        result = await self.session.execute(
            select(PTMBooking).where(PTMBooking.id == booking_id, PTMBooking.school_id == school_id)
        )
        booking = result.scalar_one_or_none()
        if not booking:
            raise PTMServiceError("Booking not found")
        if parent_id and booking.parent_id != parent_id:
            raise PTMServiceError("This is not your booking")
        if teacher_id and booking.teacher_id != teacher_id:
            raise PTMServiceError("This is not your meeting to cancel")
        if booking.status != PTMBookingStatus.CONFIRMED:
            raise PTMServiceError("This booking is already cancelled")

        booking.status = PTMBookingStatus.CANCELLED
        booking.cancelled_by = cancelled_by
        booking.cancellation_reason = reason
        booking.cancelled_at = datetime.utcnow()
        self.session.add(booking)

        slot_result = await self.session.execute(select(PTMSlot).where(PTMSlot.id == booking.slot_id))
        slot = slot_result.scalar_one_or_none()
        if slot and slot.status == PTMSlotStatus.BOOKED:
            slot.status = PTMSlotStatus.OPEN
            slot.updated_at = datetime.utcnow()
            self.session.add(slot)

        await self.session.commit()
        await self.session.refresh(booking)
        return booking
