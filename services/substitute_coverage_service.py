"""Substitute-teacher coverage — for an APPROVED leave request, find which of
the absent teacher's recurring timetabled periods fall within the leave date
range, and let an admin assign a covering teacher per period (or bulk-assign
one substitute to all of them).

Coverage is computed against the teacher's normal weekly Timetable rows, not
per calendar date — this codebase has no concept of a per-date timetable
exception (see models/timetable.py), so "Monday Period 3" is either covered
for the whole leave or not; there's no per-occurrence tracking to hang a
substitute off of.
"""
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from sqlalchemy.exc import IntegrityError

from models.leave_request import LeaveRequest, LeaveRequestStatus
from models.timetable import Timetable, Period, DayOfWeek
from models.school import AcademicTerm
from models.classroom import Class, Subject
from models.staff import Staff, StaffType
from models.substitute_assignment import SubstituteAssignment
from services.attendance_shared import working_days_in_range, fetch_holiday_dates

logger = logging.getLogger(__name__)


def _weekdays_in_range(start_date: str, end_date: str, holiday_dates: Optional[set] = None) -> set:
    """Python weekday() 0=Monday..4=Friday lines up positionally with
    DayOfWeek's declaration order (MONDAY..FRIDAY), so index into it."""
    days = list(DayOfWeek)
    weekdays = set()
    for date_str in working_days_in_range(start_date, end_date, holiday_dates):
        weekday_index = datetime.strptime(date_str, "%Y-%m-%d").weekday()
        if weekday_index < len(days):
            weekdays.add(days[weekday_index])
    return weekdays


class SubstituteCoverageService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _get_leave_request(self, school_id: str, leave_request_id: str) -> Optional[LeaveRequest]:
        result = await self.session.execute(
            select(LeaveRequest).where(LeaveRequest.id == leave_request_id, LeaveRequest.school_id == school_id)
        )
        return result.scalar_one_or_none()

    async def get_coverage_needs(self, school_id: str, leave_request_id: str) -> Dict[str, Any]:
        request = await self._get_leave_request(school_id, leave_request_id)
        if not request:
            return {"success": False, "error": "Leave request not found"}
        if request.status != LeaveRequestStatus.APPROVED:
            return {"success": False, "error": "Coverage can only be planned for an approved leave request"}

        holiday_dates = await fetch_holiday_dates(self.session, school_id)
        weekdays = _weekdays_in_range(request.start_date, request.end_date, holiday_dates)
        if not weekdays:
            return {"success": True, "periods": []}

        terms_result = await self.session.execute(
            select(AcademicTerm).where(
                AcademicTerm.school_id == school_id,
                AcademicTerm.start_date <= request.end_date,
                AcademicTerm.end_date >= request.start_date,
            )
        )
        term_ids = [t.id for t in terms_result.scalars().all()]
        if not term_ids:
            return {"success": True, "periods": []}

        entries_result = await self.session.execute(
            select(Timetable, Period, Class, Subject)
            .join(Period, Timetable.period_id == Period.id)
            .join(Class, Timetable.class_id == Class.id)
            .join(Subject, Timetable.subject_id == Subject.id)
            .where(
                Timetable.school_id == school_id,
                Timetable.teacher_id == request.staff_id,
                Timetable.day_of_week.in_(weekdays),
                Timetable.academic_term_id.in_(term_ids),
            )
            .order_by(Period.period_number)
        )
        rows = entries_result.all()

        assignments_result = await self.session.execute(
            select(SubstituteAssignment).where(SubstituteAssignment.leave_request_id == leave_request_id)
        )
        assignments_by_entry = {a.timetable_entry_id: a for a in assignments_result.scalars().all()}

        substitute_ids = {a.substitute_teacher_id for a in assignments_by_entry.values()}
        substitutes_by_id = {}
        if substitute_ids:
            staff_result = await self.session.execute(select(Staff).where(Staff.id.in_(substitute_ids)))
            substitutes_by_id = {s.id: f"{s.first_name} {s.last_name}" for s in staff_result.scalars().all()}

        periods = []
        for timetable, period, class_, subject in rows:
            assignment = assignments_by_entry.get(timetable.id)
            periods.append({
                "timetable_entry_id": timetable.id,
                "day_of_week": timetable.day_of_week,
                "period_name": period.name,
                "start_time": period.start_time,
                "end_time": period.end_time,
                "class_name": class_.name,
                "subject_name": subject.name,
                "room": timetable.room,
                "substitute_teacher_id": assignment.substitute_teacher_id if assignment else None,
                "substitute_teacher_name": substitutes_by_id.get(assignment.substitute_teacher_id) if assignment else None,
            })

        return {"success": True, "periods": periods}

    async def _check_substitute_conflict(
        self, school_id: str, substitute_teacher_id: str, timetable: Timetable
    ) -> Optional[str]:
        conflict_result = await self.session.execute(
            select(Timetable).where(
                Timetable.school_id == school_id,
                Timetable.teacher_id == substitute_teacher_id,
                Timetable.period_id == timetable.period_id,
                Timetable.day_of_week == timetable.day_of_week,
                Timetable.academic_term_id == timetable.academic_term_id,
            )
        )
        if conflict_result.scalar_one_or_none():
            return "This teacher already has a class scheduled at that same time"
        return None

    async def assign_substitute(
        self, school_id: str, leave_request_id: str, timetable_entry_id: str,
        substitute_teacher_id: str, assigned_by: str,
    ) -> Dict[str, Any]:
        try:
            request = await self._get_leave_request(school_id, leave_request_id)
            if not request:
                return {"success": False, "error": "Leave request not found"}
            if request.status != LeaveRequestStatus.APPROVED:
                return {"success": False, "error": "Coverage can only be assigned for an approved leave request"}

            timetable_result = await self.session.execute(
                select(Timetable).where(Timetable.id == timetable_entry_id, Timetable.school_id == school_id)
            )
            timetable = timetable_result.scalar_one_or_none()
            if not timetable:
                return {"success": False, "error": "Timetable entry not found"}
            if timetable.teacher_id != request.staff_id:
                return {"success": False, "error": "This period does not belong to the teacher on leave"}
            if substitute_teacher_id == request.staff_id:
                return {"success": False, "error": "The substitute must be a different teacher from the one on leave"}

            substitute_result = await self.session.execute(
                select(Staff).where(Staff.id == substitute_teacher_id, Staff.school_id == school_id)
            )
            if not substitute_result.scalar_one_or_none():
                return {"success": False, "error": "Substitute teacher not found in this school"}

            conflict = await self._check_substitute_conflict(school_id, substitute_teacher_id, timetable)
            if conflict:
                return {"success": False, "error": conflict}

            existing_result = await self.session.execute(
                select(SubstituteAssignment).where(
                    SubstituteAssignment.leave_request_id == leave_request_id,
                    SubstituteAssignment.timetable_entry_id == timetable_entry_id,
                )
            )
            existing = existing_result.scalar_one_or_none()
            if existing:
                existing.substitute_teacher_id = substitute_teacher_id
                existing.assigned_by = assigned_by
                existing.created_at = datetime.utcnow()
                self.session.add(existing)
            else:
                self.session.add(SubstituteAssignment(
                    school_id=school_id,
                    leave_request_id=leave_request_id,
                    timetable_entry_id=timetable_entry_id,
                    original_teacher_id=request.staff_id,
                    substitute_teacher_id=substitute_teacher_id,
                    assigned_by=assigned_by,
                ))
            try:
                await self.session.commit()
            except IntegrityError:
                await self.session.rollback()
                return {"success": False, "error": "This period was just assigned by someone else — please retry"}

            return {"success": True, "message": "Substitute assigned"}
        except Exception as e:
            logger.error(f"Error assigning substitute: {str(e)}")
            await self.session.rollback()
            return {"success": False, "error": str(e)}

    async def bulk_assign_substitute(
        self, school_id: str, leave_request_id: str, substitute_teacher_id: str, assigned_by: str,
    ) -> Dict[str, Any]:
        needs = await self.get_coverage_needs(school_id, leave_request_id)
        if not needs["success"]:
            return needs

        assigned = 0
        conflicts = []
        for period in needs["periods"]:
            result = await self.assign_substitute(
                school_id, leave_request_id, period["timetable_entry_id"], substitute_teacher_id, assigned_by,
            )
            if result["success"]:
                assigned += 1
            else:
                conflicts.append({
                    "period_name": period["period_name"],
                    "day_of_week": period["day_of_week"],
                    "class_name": period["class_name"],
                    "error": result["error"],
                })

        return {"success": True, "assigned": assigned, "conflicts": conflicts}

    async def get_unassigned_coverage_summary(self, school_id: str) -> Dict[str, Any]:
        """School-wide "who still needs a substitute" view — previously the
        only way to see this was to already know a specific leave_request_id
        and call get_coverage_needs on it one at a time. Scans every
        currently-relevant (not yet ended) APPROVED leave request for a
        TEACHING staff member and reports which still have periods with no
        SubstituteAssignment."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        requests_result = await self.session.execute(
            select(LeaveRequest, Staff)
            .join(Staff, Staff.id == LeaveRequest.staff_id)
            .where(
                LeaveRequest.school_id == school_id,
                LeaveRequest.status == LeaveRequestStatus.APPROVED,
                LeaveRequest.end_date >= today,
                Staff.staff_type == StaffType.TEACHING,
            )
        )
        rows = requests_result.all()

        summary = []
        for request, staff in rows:
            needs = await self.get_coverage_needs(school_id, request.id)
            if not needs.get("success"):
                continue
            unassigned = [p for p in needs["periods"] if not p.get("substitute_teacher_id")]
            if unassigned:
                summary.append({
                    "leave_request_id": request.id,
                    "staff_id": staff.id,
                    "staff_name": f"{staff.first_name} {staff.last_name}",
                    "start_date": request.start_date,
                    "end_date": request.end_date,
                    "unassigned_period_count": len(unassigned),
                })

        return {"success": True, "leave_requests_with_gaps": summary}

    async def remove_assignment(self, school_id: str, leave_request_id: str, timetable_entry_id: str) -> Dict[str, Any]:
        try:
            result = await self.session.execute(
                select(SubstituteAssignment).where(
                    SubstituteAssignment.school_id == school_id,
                    SubstituteAssignment.leave_request_id == leave_request_id,
                    SubstituteAssignment.timetable_entry_id == timetable_entry_id,
                )
            )
            assignment = result.scalar_one_or_none()
            if not assignment:
                return {"success": False, "error": "No substitute assignment found for this period"}
            await self.session.delete(assignment)
            await self.session.commit()
            return {"success": True, "message": "Substitute assignment removed"}
        except Exception as e:
            logger.error(f"Error removing substitute assignment: {str(e)}")
            await self.session.rollback()
            return {"success": False, "error": str(e)}
