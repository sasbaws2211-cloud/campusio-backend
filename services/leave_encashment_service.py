"""Leave Encashment Service - manual leave payout requests and payroll payment"""
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from models.leave_encashment import (
    LeaveEncashmentRequest, LeaveEncashmentCreate, LeaveEncashmentStatus
)
from models.leave_request import LeaveBalance, LeaveType
from models.payroll import PayrollAdjustment

logger = logging.getLogger(__name__)

# Divisor used to derive a daily rate from basic_salary, matching how the
# rest of payroll already treats "monthly" as the base unit (contracts are
# priced per calendar month, not per working day).
DAYS_PER_MONTH = 30

# Only unused ANNUAL leave is encashable — matches standard HR practice and
# leave_request_service.py's own BALANCE_TRACKED_TYPES, which likewise never
# lets sick/casual leave be converted to cash.
ENCASHABLE_LEAVE_TYPE = LeaveType.ANNUAL


class LeaveEncashmentService:
    """Service for leave encashment requests and their payroll payout.

    `leave_days` is entered manually by HR/Admin, but is validated against
    (and, on approval, deducted from) the staff member's ANNUAL LeaveBalance
    row — the same balance ledger leave_request_service.py maintains.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def _get_balance(self, school_id: str, staff_id: str, year: int) -> Optional[LeaveBalance]:
        result = await self.session.execute(
            select(LeaveBalance).where(
                LeaveBalance.school_id == school_id,
                LeaveBalance.staff_id == staff_id,
                LeaveBalance.leave_type == ENCASHABLE_LEAVE_TYPE,
                LeaveBalance.year == year,
            )
        )
        return result.scalar_one_or_none()

    async def create_request(
        self,
        school_id: str,
        data: LeaveEncashmentCreate,
        requested_by: str,
    ) -> Dict[str, Any]:
        """Create a pending leave encashment request. daily_rate is derived
        from the staff member's active PayrollContract.basic_salary at
        request time — if their salary changes later, an already-created
        request keeps the rate it was quoted at."""
        from services.payroll_service import PayrollService

        try:
            year = datetime.utcnow().year
            balance = await self._get_balance(school_id, data.staff_id, year)
            if not balance:
                return {
                    "success": False,
                    "error": f"No annual leave balance found for {year} — ask HR to seed entitlements first",
                }
            remaining = balance.entitlement_days - balance.used_days
            if data.leave_days > remaining:
                return {
                    "success": False,
                    "error": f"Insufficient annual leave balance: requesting {data.leave_days} day(s), only {remaining} remaining",
                }

            payroll_service = PayrollService(self.session)
            contract = await payroll_service.get_active_contract(school_id, data.staff_id)
            if not contract:
                return {"success": False, "error": "Staff member has no active payroll contract"}

            daily_rate = round(float(contract.basic_salary) / DAYS_PER_MONTH, 2)
            encashment_amount = round(daily_rate * data.leave_days, 2)

            request = LeaveEncashmentRequest(
                school_id=school_id,
                staff_id=data.staff_id,
                leave_days=data.leave_days,
                daily_rate=daily_rate,
                encashment_amount=encashment_amount,
                reason=data.reason,
                requested_by=requested_by,
                status=LeaveEncashmentStatus.PENDING,
            )
            self.session.add(request)
            await self.session.commit()
            await self.session.refresh(request)
            return {
                "success": True,
                "request_id": request.id,
                "encashment_amount": encashment_amount,
                "message": "Leave encashment request created, pending approval",
            }
        except Exception as e:
            logger.error(f"Error creating leave encashment request: {str(e)}")
            await self.session.rollback()
            return {"success": False, "error": str(e)}

    async def get_request(self, school_id: str, request_id: str) -> Optional[LeaveEncashmentRequest]:
        result = await self.session.execute(
            select(LeaveEncashmentRequest).where(
                LeaveEncashmentRequest.id == request_id,
                LeaveEncashmentRequest.school_id == school_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_requests(
        self,
        school_id: str,
        staff_id: Optional[str] = None,
        status: Optional[LeaveEncashmentStatus] = None,
    ) -> List[LeaveEncashmentRequest]:
        query = select(LeaveEncashmentRequest).where(LeaveEncashmentRequest.school_id == school_id)
        if staff_id:
            query = query.where(LeaveEncashmentRequest.staff_id == staff_id)
        if status:
            query = query.where(LeaveEncashmentRequest.status == status)
        query = query.order_by(LeaveEncashmentRequest.created_at.desc())
        result = await self.session.execute(query)
        return result.scalars().all()

    async def approve_request(self, school_id: str, request_id: str, approved_by: str) -> Dict[str, Any]:
        """Approve a request — does not pay it out yet, only marks it
        eligible for the next payroll run generated for this staff member."""
        try:
            request = await self.get_request(school_id, request_id)
            if not request:
                return {"success": False, "error": "Request not found"}
            if request.status != LeaveEncashmentStatus.PENDING:
                return {"success": False, "error": f"Cannot approve a request in {request.status} status"}
            if request.requested_by == approved_by:
                return {"success": False, "error": "You cannot approve a leave encashment request you submitted yourself — ask another admin to approve it"}

            # Re-check at approval time — the balance may have shrunk from
            # other approvals (leave requests or encashments) made since
            # this request was filed.
            year = request.created_at.year
            balance = await self._get_balance(school_id, request.staff_id, year)
            if not balance:
                return {"success": False, "error": f"No annual leave balance found for {year}"}
            remaining = balance.entitlement_days - balance.used_days
            if request.leave_days > remaining:
                return {
                    "success": False,
                    "error": f"Insufficient balance at approval time: {request.leave_days} day(s) requested, only {remaining} remaining",
                }
            balance.used_days += request.leave_days
            balance.updated_at = datetime.utcnow()
            self.session.add(balance)

            request.status = LeaveEncashmentStatus.APPROVED
            request.approved_by = approved_by
            request.approved_at = datetime.utcnow()
            request.updated_at = datetime.utcnow()
            self.session.add(request)
            await self.session.commit()
            return {"success": True, "message": "Leave encashment approved"}
        except Exception as e:
            logger.error(f"Error approving leave encashment request: {str(e)}")
            await self.session.rollback()
            return {"success": False, "error": str(e)}

    async def reject_request(self, school_id: str, request_id: str, rejected_by: str) -> Dict[str, Any]:
        try:
            request = await self.get_request(school_id, request_id)
            if not request:
                return {"success": False, "error": "Request not found"}
            if request.status != LeaveEncashmentStatus.PENDING:
                return {"success": False, "error": f"Cannot reject a request in {request.status} status"}

            request.status = LeaveEncashmentStatus.REJECTED
            request.approved_by = rejected_by
            request.approved_at = datetime.utcnow()
            request.updated_at = datetime.utcnow()
            self.session.add(request)
            await self.session.commit()
            return {"success": True, "message": "Leave encashment rejected"}
        except Exception as e:
            logger.error(f"Error rejecting leave encashment request: {str(e)}")
            await self.session.rollback()
            return {"success": False, "error": str(e)}

    async def get_approved_unpaid_for_staff(
        self, school_id: str, staff_id: str
    ) -> List[LeaveEncashmentRequest]:
        result = await self.session.execute(
            select(LeaveEncashmentRequest).where(
                LeaveEncashmentRequest.school_id == school_id,
                LeaveEncashmentRequest.staff_id == staff_id,
                LeaveEncashmentRequest.status == LeaveEncashmentStatus.APPROVED,
            )
        )
        return result.scalars().all()

    async def mark_paid(
        self,
        school_id: str,
        payroll_run_id: str,
        staff_id: str,
        request: LeaveEncashmentRequest,
    ) -> PayrollAdjustment:
        """Pay out an approved request through a payroll run: creates a
        pre-approved PayrollAdjustment (adjustment_type='leave_encashment',
        positive amount — it's an addition to pay) that
        payroll_service._recompute_line_item_net folds into net_amount.
        Caller is responsible for calling _recompute_line_item_net
        afterwards."""
        adjustment = PayrollAdjustment(
            payroll_run_id=payroll_run_id,
            school_id=school_id,
            staff_id=staff_id,
            adjustment_type="leave_encashment",
            amount=request.encashment_amount,
            reason=f"Leave encashment: {request.leave_days} day(s) @ {request.daily_rate}/day",
            created_by="SYSTEM",
            approved_by="SYSTEM",
            approved_at=datetime.utcnow(),
        )
        self.session.add(adjustment)
        await self.session.flush()

        request.status = LeaveEncashmentStatus.PAID
        request.payroll_run_id = payroll_run_id
        request.payroll_adjustment_id = adjustment.id
        request.updated_at = datetime.utcnow()
        self.session.add(request)
        await self.session.flush()

        return adjustment
