"""Leave Request Service - staff leave applications with a balance ledger.

Mirrors services/leave_encashment_service.py's shape ({"success": bool, ...}
/ {"success": False, "error": str}, rollback on exception), but this
workflow actually maintains a balance (leave_encashment deliberately does
not) and its approval side effect writes real StaffAttendance(EXCUSED) rows
tagged with leave_request_id so they can be cleanly reversed by revoke_request.

Audit logging (audit_service.log_event) is done by the router, not here —
it needs the full current_user object, which callers of this service already
have and this service intentionally doesn't require.
"""
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from models.leave_request import (
    LeaveRequest, LeaveRequestCreate, LeaveRequestStatus,
    LeaveBalance, LeaveType,
)
from models.attendance import StaffAttendance, AttendanceStatus
from models.staff import Staff, StaffStatus, StaffType
from services.attendance_shared import working_days_in_range, fetch_holiday_dates

logger = logging.getLogger(__name__)

# Default annual-leave entitlement varies by staff type (teaching staff
# already get long school-break periods off, so a lower base is typical);
# sick/casual are flat across staff types. These are seeding defaults only —
# HR can override any individual staff member's entitlement afterwards via
# update_balance.
DEFAULT_ANNUAL_ENTITLEMENT_BY_STAFF_TYPE = {
    StaffType.TEACHING: 18,
    StaffType.NON_TEACHING: 21,
    StaffType.ADMIN: 21,
}
DEFAULT_SICK_ENTITLEMENT_DAYS = 10
DEFAULT_CASUAL_ENTITLEMENT_DAYS = 5

BALANCE_TRACKED_TYPES = (LeaveType.ANNUAL, LeaveType.SICK, LeaveType.CASUAL)


def _leave_type_str(value) -> str:
    """leave_type is stored native_enum=False with bare string labels (not
    bound to the LeaveType class — same pattern as Staff.payout_verification_
    status), so SQLAlchemy round-trips it as a plain str, not a LeaveType
    instance. A freshly-constructed LeaveRequest still holds a real LeaveType
    until it's re-fetched, so handle both like audit_service.py does."""
    return value.value if hasattr(value, "value") else str(value)


def _default_entitlement(staff_type: StaffType, leave_type: LeaveType) -> float:
    if leave_type == LeaveType.ANNUAL:
        return float(DEFAULT_ANNUAL_ENTITLEMENT_BY_STAFF_TYPE.get(staff_type, 18))
    if leave_type == LeaveType.SICK:
        return float(DEFAULT_SICK_ENTITLEMENT_DAYS)
    if leave_type == LeaveType.CASUAL:
        return float(DEFAULT_CASUAL_ENTITLEMENT_DAYS)
    return 0.0


class LeaveRequestService:
    def __init__(self, session: AsyncSession):
        self.session = session

    # ---- Requests ----

    async def create_request(
        self, school_id: str, data: LeaveRequestCreate, staff_id: str, requested_by: str
    ) -> Dict[str, Any]:
        try:
            staff_result = await self.session.execute(
                select(Staff).where(Staff.id == staff_id, Staff.school_id == school_id)
            )
            staff = staff_result.scalar_one_or_none()
            if not staff:
                return {"success": False, "error": "Staff member not found"}

            try:
                start = datetime.strptime(data.start_date, "%Y-%m-%d")
                end = datetime.strptime(data.end_date, "%Y-%m-%d")
            except ValueError:
                return {"success": False, "error": "Dates must be in YYYY-MM-DD format"}
            if end < start:
                return {"success": False, "error": "end_date cannot be before start_date"}
            if start.year != end.year:
                return {"success": False, "error": "Leave requests spanning more than one calendar year are not supported — submit separate requests per year"}

            holiday_dates = await fetch_holiday_dates(self.session, school_id)
            days_requested = len(working_days_in_range(data.start_date, data.end_date, holiday_dates))
            if days_requested == 0:
                return {"success": False, "error": "This date range contains no working days"}

            # Reject an overlapping request while an earlier one for the same
            # staff member is still active (PENDING/MANAGER_APPROVED/APPROVED).
            # Without this, two overlapping requests could each be validated
            # against the same starting balance (nothing is deducted until
            # final approval), then both approved independently and each
            # deduct their own days from used_days -- double-consuming the
            # balance for what is physically the same block of days off.
            # ISO "YYYY-MM-DD" strings compare correctly as plain text.
            overlap_result = await self.session.execute(
                select(LeaveRequest).where(
                    LeaveRequest.staff_id == staff_id,
                    LeaveRequest.school_id == school_id,
                    LeaveRequest.status.in_([
                        LeaveRequestStatus.PENDING, LeaveRequestStatus.MANAGER_APPROVED, LeaveRequestStatus.APPROVED,
                    ]),
                    LeaveRequest.start_date <= data.end_date,
                    LeaveRequest.end_date >= data.start_date,
                )
            )
            if overlap_result.scalar_one_or_none():
                return {"success": False, "error": "You already have an active leave request that overlaps these dates"}

            if data.leave_type in BALANCE_TRACKED_TYPES:
                balance = await self._get_balance(school_id, staff_id, data.leave_type, start.year)
                if not balance:
                    return {
                        "success": False,
                        "error": f"No {_leave_type_str(data.leave_type)} leave balance found for {start.year} — ask HR to seed entitlements first",
                    }
                remaining = balance.entitlement_days - balance.used_days
                if days_requested > remaining:
                    return {
                        "success": False,
                        "error": f"Insufficient balance: requesting {days_requested} day(s), only {remaining} remaining",
                    }

            request = LeaveRequest(
                school_id=school_id,
                staff_id=staff_id,
                leave_type=data.leave_type,
                start_date=data.start_date,
                end_date=data.end_date,
                days_requested=days_requested,
                reason=data.reason,
                requested_by=requested_by,
                status=LeaveRequestStatus.PENDING,
            )
            self.session.add(request)
            await self.session.commit()
            await self.session.refresh(request)
            return {
                "success": True,
                "request_id": request.id,
                "days_requested": days_requested,
                "message": "Leave request submitted, pending approval",
            }
        except Exception as e:
            logger.error(f"Error creating leave request: {str(e)}")
            await self.session.rollback()
            return {"success": False, "error": str(e)}

    async def get_request(self, school_id: str, request_id: str) -> Optional[LeaveRequest]:
        result = await self.session.execute(
            select(LeaveRequest).where(LeaveRequest.id == request_id, LeaveRequest.school_id == school_id)
        )
        return result.scalar_one_or_none()

    async def list_requests(
        self,
        school_id: str,
        staff_id: Optional[str] = None,
        status: Optional[LeaveRequestStatus] = None,
        leave_type: Optional[LeaveType] = None,
    ) -> List[LeaveRequest]:
        query = select(LeaveRequest).where(LeaveRequest.school_id == school_id)
        if staff_id:
            query = query.where(LeaveRequest.staff_id == staff_id)
        if status:
            query = query.where(LeaveRequest.status == status)
        if leave_type:
            query = query.where(LeaveRequest.leave_type == leave_type)
        query = query.order_by(LeaveRequest.created_at.desc())
        result = await self.session.execute(query)
        return result.scalars().all()

    async def manager_approve_request(self, school_id: str, request_id: str, manager_user_id: str) -> Dict[str, Any]:
        """First step of the chain — only reached when the requester has a
        Staff.manager_id set (see routers/leave_requests.py, which checks
        that before calling this). Doesn't touch the balance or attendance
        — those side effects only happen at final approve_request."""
        try:
            request = await self.get_request(school_id, request_id)
            if not request:
                return {"success": False, "error": "Request not found"}
            if request.status != LeaveRequestStatus.PENDING:
                return {"success": False, "error": f"Cannot manager-approve a request in {request.status} status"}

            request.status = LeaveRequestStatus.MANAGER_APPROVED
            request.manager_approved_by = manager_user_id
            request.manager_approved_at = datetime.utcnow()
            request.updated_at = datetime.utcnow()
            self.session.add(request)
            await self.session.commit()
            return {"success": True, "message": "Manager approval recorded — now pending HR/admin final approval"}
        except Exception as e:
            logger.error(f"Error recording manager approval: {str(e)}")
            await self.session.rollback()
            return {"success": False, "error": str(e)}

    async def manager_reject_request(self, school_id: str, request_id: str, manager_user_id: str, reason: Optional[str] = None) -> Dict[str, Any]:
        try:
            request = await self.get_request(school_id, request_id)
            if not request:
                return {"success": False, "error": "Request not found"}
            if request.status != LeaveRequestStatus.PENDING:
                return {"success": False, "error": f"Cannot manager-reject a request in {request.status} status"}

            request.status = LeaveRequestStatus.REJECTED
            request.manager_rejection_reason = reason
            request.rejection_reason = reason
            request.approved_by = manager_user_id  # who actioned it, same field the final-reject path uses
            request.approved_at = datetime.utcnow()
            request.updated_at = datetime.utcnow()
            self.session.add(request)
            await self.session.commit()
            return {"success": True, "message": "Leave request rejected by manager"}
        except Exception as e:
            logger.error(f"Error recording manager rejection: {str(e)}")
            await self.session.rollback()
            return {"success": False, "error": str(e)}

    async def approve_request(self, school_id: str, request_id: str, approved_by: str) -> Dict[str, Any]:
        try:
            request = await self.get_request(school_id, request_id)
            if not request:
                return {"success": False, "error": "Request not found"}
            if request.status not in (LeaveRequestStatus.PENDING, LeaveRequestStatus.MANAGER_APPROVED):
                return {"success": False, "error": f"Cannot approve a request in {request.status} status"}

            year = datetime.strptime(request.start_date, "%Y-%m-%d").year
            if request.leave_type in BALANCE_TRACKED_TYPES:
                # Re-check at approval time — the balance may have shrunk
                # from other approvals made since this request was filed.
                balance = await self._get_balance(school_id, request.staff_id, request.leave_type, year)
                if not balance:
                    return {"success": False, "error": f"No {_leave_type_str(request.leave_type)} leave balance found for {year}"}
                remaining = balance.entitlement_days - balance.used_days
                if request.days_requested > remaining:
                    return {
                        "success": False,
                        "error": f"Insufficient balance at approval time: {request.days_requested} day(s) requested, only {remaining} remaining",
                    }
                balance.used_days += request.days_requested
                balance.updated_at = datetime.utcnow()
                self.session.add(balance)

            marked_excused = 0
            skipped_existing = 0
            holiday_dates = await fetch_holiday_dates(self.session, school_id)
            for date_str in working_days_in_range(request.start_date, request.end_date, holiday_dates):
                existing_result = await self.session.execute(
                    select(StaffAttendance).where(
                        StaffAttendance.staff_id == request.staff_id,
                        StaffAttendance.attendance_date == date_str,
                    )
                )
                if existing_result.scalar_one_or_none():
                    skipped_existing += 1
                    continue
                self.session.add(StaffAttendance(
                    school_id=school_id,
                    staff_id=request.staff_id,
                    attendance_date=date_str,
                    status=AttendanceStatus.EXCUSED,
                    remarks=f"Approved {_leave_type_str(request.leave_type)} leave",
                    recorded_by=approved_by,
                    leave_request_id=request.id,
                ))
                marked_excused += 1

            request.status = LeaveRequestStatus.APPROVED
            request.approved_by = approved_by
            request.approved_at = datetime.utcnow()
            request.updated_at = datetime.utcnow()
            self.session.add(request)
            await self.session.commit()

            # Surface substitute-coverage needs right at the moment of
            # approval — previously "a leave approval by itself doesn't
            # touch the timetable" (see substitute_coverage_service's own
            # docstring) meant nothing ever told the approver a teaching
            # staff member's classes now need a substitute; an admin had to
            # separately think to check. This doesn't auto-assign anyone
            # (picking a substitute is a judgment call) — it just makes the
            # gap visible immediately instead of silently.
            coverage_periods_needing_substitute = 0
            staff_result = await self.session.execute(select(Staff).where(Staff.id == request.staff_id))
            staff = staff_result.scalar_one_or_none()
            if staff and staff.staff_type == StaffType.TEACHING:
                from services.substitute_coverage_service import SubstituteCoverageService
                coverage = await SubstituteCoverageService(self.session).get_coverage_needs(school_id, request.id)
                if coverage.get("success"):
                    coverage_periods_needing_substitute = sum(
                        1 for p in coverage["periods"] if not p.get("substitute_teacher_id")
                    )

            return {
                "success": True,
                "message": "Leave request approved",
                "days_marked_excused": marked_excused,
                "days_skipped_existing": skipped_existing,
                "coverage_periods_needing_substitute": coverage_periods_needing_substitute,
            }
        except Exception as e:
            logger.error(f"Error approving leave request: {str(e)}")
            await self.session.rollback()
            return {"success": False, "error": str(e)}

    async def reject_request(
        self, school_id: str, request_id: str, rejected_by: str, reason: Optional[str] = None
    ) -> Dict[str, Any]:
        try:
            request = await self.get_request(school_id, request_id)
            if not request:
                return {"success": False, "error": "Request not found"}
            if request.status not in (LeaveRequestStatus.PENDING, LeaveRequestStatus.MANAGER_APPROVED):
                return {"success": False, "error": f"Cannot reject a request in {request.status} status"}

            request.status = LeaveRequestStatus.REJECTED
            request.rejection_reason = reason
            request.approved_by = rejected_by
            request.approved_at = datetime.utcnow()
            request.updated_at = datetime.utcnow()
            self.session.add(request)
            await self.session.commit()
            return {"success": True, "message": "Leave request rejected"}
        except Exception as e:
            logger.error(f"Error rejecting leave request: {str(e)}")
            await self.session.rollback()
            return {"success": False, "error": str(e)}

    async def cancel_request(self, school_id: str, request_id: str, staff_id: str) -> Dict[str, Any]:
        """Self-service withdrawal — only while still PENDING, so there are
        never any balance/attendance side effects to undo."""
        try:
            request = await self.get_request(school_id, request_id)
            if not request:
                return {"success": False, "error": "Request not found"}
            if request.staff_id != staff_id:
                return {"success": False, "error": "You can only cancel your own leave requests"}
            if request.status != LeaveRequestStatus.PENDING:
                return {"success": False, "error": f"Cannot cancel a request in {request.status} status"}

            request.status = LeaveRequestStatus.CANCELLED
            request.updated_at = datetime.utcnow()
            self.session.add(request)
            await self.session.commit()
            return {"success": True, "message": "Leave request cancelled"}
        except Exception as e:
            logger.error(f"Error cancelling leave request: {str(e)}")
            await self.session.rollback()
            return {"success": False, "error": str(e)}

    async def revoke_request(
        self, school_id: str, request_id: str, revoked_by: str, reason: Optional[str] = None
    ) -> Dict[str, Any]:
        """Admin-only reversal of an already-approved request: restores the
        balance and deletes every StaffAttendance row this approval created."""
        try:
            request = await self.get_request(school_id, request_id)
            if not request:
                return {"success": False, "error": "Request not found"}
            if request.status != LeaveRequestStatus.APPROVED:
                return {"success": False, "error": f"Cannot revoke a request in {request.status} status — only approved requests can be revoked"}

            if request.leave_type in BALANCE_TRACKED_TYPES:
                year = datetime.strptime(request.start_date, "%Y-%m-%d").year
                balance = await self._get_balance(school_id, request.staff_id, request.leave_type, year)
                if balance:
                    balance.used_days = max(0.0, balance.used_days - request.days_requested)
                    balance.updated_at = datetime.utcnow()
                    self.session.add(balance)

            rows_result = await self.session.execute(
                select(StaffAttendance).where(StaffAttendance.leave_request_id == request.id)
            )
            rows_deleted = 0
            for row in rows_result.scalars().all():
                await self.session.delete(row)
                rows_deleted += 1

            request.status = LeaveRequestStatus.REVOKED
            request.revoked_by = revoked_by
            request.revoked_at = datetime.utcnow()
            request.revoke_reason = reason
            request.updated_at = datetime.utcnow()
            self.session.add(request)
            await self.session.commit()

            return {"success": True, "message": "Leave request revoked", "attendance_rows_removed": rows_deleted}
        except Exception as e:
            logger.error(f"Error revoking leave request: {str(e)}")
            await self.session.rollback()
            return {"success": False, "error": str(e)}

    # ---- Balances ----

    async def _get_balance(
        self, school_id: str, staff_id: str, leave_type: LeaveType, year: int
    ) -> Optional[LeaveBalance]:
        result = await self.session.execute(
            select(LeaveBalance).where(
                LeaveBalance.school_id == school_id,
                LeaveBalance.staff_id == staff_id,
                LeaveBalance.leave_type == leave_type,
                LeaveBalance.year == year,
            )
        )
        return result.scalar_one_or_none()

    async def list_balances(
        self, school_id: str, staff_id: Optional[str] = None, year: Optional[int] = None
    ) -> List[LeaveBalance]:
        query = select(LeaveBalance).where(LeaveBalance.school_id == school_id)
        if staff_id:
            query = query.where(LeaveBalance.staff_id == staff_id)
        if year:
            query = query.where(LeaveBalance.year == year)
        result = await self.session.execute(query)
        return result.scalars().all()

    async def get_balance(self, school_id: str, balance_id: str) -> Optional[LeaveBalance]:
        result = await self.session.execute(
            select(LeaveBalance).where(LeaveBalance.id == balance_id, LeaveBalance.school_id == school_id)
        )
        return result.scalar_one_or_none()

    async def update_balance(self, school_id: str, balance_id: str, entitlement_days: float) -> Dict[str, Any]:
        try:
            balance = await self.get_balance(school_id, balance_id)
            if not balance:
                return {"success": False, "error": "Leave balance not found"}
            balance.entitlement_days = entitlement_days
            balance.updated_at = datetime.utcnow()
            self.session.add(balance)
            await self.session.commit()
            return {"success": True, "message": "Leave balance updated"}
        except Exception as e:
            logger.error(f"Error updating leave balance: {str(e)}")
            await self.session.rollback()
            return {"success": False, "error": str(e)}

    async def seed_balances(
        self, school_id: str, year: int, staff_ids: Optional[List[str]], seeded_by: str
    ) -> Dict[str, Any]:
        """Create default ANNUAL/SICK/CASUAL balance rows for a year. Skips
        any (staff, leave_type, year) combination that already has a row —
        never overwrites an existing balance, so this is safe to re-run."""
        try:
            query = select(Staff).where(Staff.school_id == school_id, Staff.status == StaffStatus.ACTIVE)
            if staff_ids:
                query = query.where(Staff.id.in_(staff_ids))
            staff_result = await self.session.execute(query)
            staff_list = staff_result.scalars().all()

            created = 0
            skipped_existing = 0
            for staff in staff_list:
                for leave_type in BALANCE_TRACKED_TYPES:
                    existing = await self._get_balance(school_id, staff.id, leave_type, year)
                    if existing:
                        skipped_existing += 1
                        continue
                    self.session.add(LeaveBalance(
                        school_id=school_id,
                        staff_id=staff.id,
                        leave_type=leave_type,
                        year=year,
                        entitlement_days=_default_entitlement(staff.staff_type, leave_type),
                    ))
                    created += 1

            await self.session.commit()
            return {"success": True, "created": created, "skipped_existing": skipped_existing}
        except Exception as e:
            logger.error(f"Error seeding leave balances: {str(e)}")
            await self.session.rollback()
            return {"success": False, "error": str(e)}
