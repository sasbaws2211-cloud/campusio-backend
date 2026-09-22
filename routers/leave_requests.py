"""Staff leave-request router — self-service submission/history plus
admin/HR approve-reject-revoke, and balance seeding/overrides. Reuses
STAFF_ATTENDANCE_ADMIN_ROLES from routers.attendance since this workflow's
primary side effect (StaffAttendance EXCUSED rows) lives in that module.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from typing import Optional

from models.leave_request import (
    LeaveRequest, LeaveRequestCreate, LeaveRequestStatus, LeaveType,
    LeaveRequestReject, LeaveRequestRevoke,
    LeaveBalance, LeaveBalanceSeedRequest, LeaveBalanceUpdate,
)
from models.staff import Staff
from models.user import User
from database import get_session
from auth import get_current_user, require_permission
from dependencies import assert_campus_access
from services.leave_request_service import LeaveRequestService
from services.audit_service import log_event
from routers.attendance import STAFF_ATTENDANCE_ADMIN_ROLES

router = APIRouter(tags=["Leave Requests"])


def _serialize_request(r: LeaveRequest) -> dict:
    return {
        "id": r.id,
        "staff_id": r.staff_id,
        "leave_type": r.leave_type,
        "start_date": r.start_date,
        "end_date": r.end_date,
        "days_requested": r.days_requested,
        "reason": r.reason,
        "status": r.status,
        "requested_by": r.requested_by,
        "manager_approved_by": r.manager_approved_by,
        "manager_approved_at": r.manager_approved_at.isoformat() if r.manager_approved_at else None,
        "manager_rejection_reason": r.manager_rejection_reason,
        "approved_by": r.approved_by,
        "approved_at": r.approved_at.isoformat() if r.approved_at else None,
        "rejection_reason": r.rejection_reason,
        "revoked_by": r.revoked_by,
        "revoked_at": r.revoked_at.isoformat() if r.revoked_at else None,
        "revoke_reason": r.revoke_reason,
        "created_at": r.created_at.isoformat(),
    }


def _serialize_balance(b: LeaveBalance) -> dict:
    return {
        "id": b.id,
        "staff_id": b.staff_id,
        "leave_type": b.leave_type,
        "year": b.year,
        "entitlement_days": b.entitlement_days,
        "used_days": b.used_days,
        "remaining_days": b.entitlement_days - b.used_days,
    }


async def _resolve_own_staff(session: AsyncSession, current_user: User) -> Staff:
    result = await session.execute(select(Staff).where(Staff.user_id == current_user.id))
    staff = result.scalar_one_or_none()
    if not staff:
        raise HTTPException(status_code=404, detail="No staff profile linked to this account")
    return staff


async def _resolve_staff_by_id(session: AsyncSession, current_user: User, staff_id: str) -> Staff:
    result = await session.execute(select(Staff).where(Staff.id == staff_id, Staff.school_id == current_user.school_id))
    staff = result.scalar_one_or_none()
    if not staff:
        raise HTTPException(status_code=404, detail="Staff member not found")
    assert_campus_access(current_user, staff.campus_id)
    return staff


async def _is_authorized_approver(session: AsyncSession, school_id: str, requester: Staff, candidate_staff_id: str) -> bool:
    """Who counts as `requester`'s manager-approval-step approver.
    Staff.manager_id is checked first (as before); previously a requester
    with no manager_id set skipped this step entirely, even if their
    Department has a registered head_staff_id — Department.head_staff_id
    was never consulted anywhere as an approval gate. Only falls back to
    the department head when manager_id is genuinely unset, so a school
    that already relies on manager_id sees no change."""
    if requester.manager_id == candidate_staff_id:
        return True
    if requester.manager_id is None and requester.department:
        from models.department import Department
        dept = (await session.execute(
            select(Department).where(Department.school_id == school_id, Department.name == requester.department)
        )).scalar_one_or_none()
        if dept and dept.head_staff_id == candidate_staff_id:
            return True
    return False


# ── Leave requests ──────────────────────────────────────────────────────────

@router.post("/leave-requests", response_model=dict)
async def create_leave_request(
    data: LeaveRequestCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if not current_user.school_id:
        raise HTTPException(status_code=403, detail="No school context")

    if data.staff_id and current_user.role in STAFF_ATTENDANCE_ADMIN_ROLES:
        staff = await _resolve_staff_by_id(session, current_user, data.staff_id)
    else:
        staff = await _resolve_own_staff(session, current_user)

    service = LeaveRequestService(session)
    result = await service.create_request(current_user.school_id, data, staff.id, current_user.id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/leave-requests/my", response_model=dict)
async def list_my_leave_requests(
    status: Optional[LeaveRequestStatus] = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    staff = await _resolve_own_staff(session, current_user)
    service = LeaveRequestService(session)
    requests = await service.list_requests(current_user.school_id, staff_id=staff.id, status=status)
    return {"records": [_serialize_request(r) for r in requests]}


@router.get("/leave-requests", response_model=dict)
async def list_leave_requests(
    staff_id: Optional[str] = None,
    status: Optional[LeaveRequestStatus] = None,
    leave_type: Optional[LeaveType] = None,
    current_user: User = Depends(require_permission("hr.leave_request.view")),
    session: AsyncSession = Depends(get_session),
):
    if not current_user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    service = LeaveRequestService(session)
    requests = await service.list_requests(current_user.school_id, staff_id=staff_id, status=status, leave_type=leave_type)
    return {"records": [_serialize_request(r) for r in requests]}


@router.get("/leave-requests/calendar", response_model=dict)
async def leave_calendar(
    start_date: str,
    end_date: str,
    current_user: User = Depends(require_permission("hr.leave_request.view")),
    session: AsyncSession = Depends(get_session),
):
    """Approved leave in a date range, with staff names resolved — the
    data behind a calendar view (frontend renders the grid; this just
    returns each entry once, unlike expanding to one row per day). Must
    stay registered before GET /leave-requests/{request_id} — that's a
    single-segment catch-all that would otherwise swallow this path,
    treating "calendar" as a request_id."""
    if not current_user.school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(LeaveRequest).where(
            LeaveRequest.school_id == current_user.school_id,
            LeaveRequest.status == LeaveRequestStatus.APPROVED,
            LeaveRequest.start_date <= end_date,
            LeaveRequest.end_date >= start_date,
        ).order_by(LeaveRequest.start_date)
    )
    requests = result.scalars().all()

    staff_ids = list({r.staff_id for r in requests})
    staff_map = {}
    if staff_ids:
        staff_result = await session.execute(select(Staff).where(Staff.id.in_(staff_ids)))
        staff_map = {s.id: s for s in staff_result.scalars().all()}

    return {
        "records": [
            {
                "id": r.id, "staff_id": r.staff_id,
                "staff_name": f"{staff_map[r.staff_id].first_name} {staff_map[r.staff_id].last_name}" if r.staff_id in staff_map else "Unknown",
                "leave_type": r.leave_type, "start_date": r.start_date, "end_date": r.end_date,
            }
            for r in requests
        ]
    }


@router.get("/leave-requests/pending-my-approval", response_model=dict)
async def list_requests_pending_my_approval(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Every PENDING request from a staff member whose manager_id points
    at the caller's own Staff row — the manager's action queue. Must stay
    registered before GET /leave-requests/{request_id} — same single-
    segment catch-all collision as /calendar above."""
    manager_staff = await _resolve_own_staff(session, current_user)
    reports_result = await session.execute(select(Staff.id).where(Staff.manager_id == manager_staff.id))
    report_ids = [row[0] for row in reports_result.all()]
    if not report_ids:
        return {"records": []}

    result = await session.execute(
        select(LeaveRequest).where(
            LeaveRequest.school_id == current_user.school_id,
            LeaveRequest.staff_id.in_(report_ids),
            LeaveRequest.status == LeaveRequestStatus.PENDING,
        ).order_by(LeaveRequest.created_at)
    )
    return {"records": [_serialize_request(r) for r in result.scalars().all()]}


@router.get("/leave-requests/{request_id}", response_model=dict)
async def get_leave_request(
    request_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    service = LeaveRequestService(session)
    request = await service.get_request(current_user.school_id, request_id)
    if not request:
        raise HTTPException(status_code=404, detail="Leave request not found")

    is_admin = current_user.role in STAFF_ATTENDANCE_ADMIN_ROLES
    if not is_admin:
        staff = await _resolve_own_staff(session, current_user)
        if staff.id != request.staff_id:
            raise HTTPException(status_code=403, detail="Access denied")
    return _serialize_request(request)


@router.post("/leave-requests/{request_id}/manager-approve", response_model=dict)
async def manager_approve_leave_request(
    request_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """First step of the approval chain — the requester's own manager
    (Staff.manager_id), or, when no manager_id is set, their registered
    Department head (see _is_authorized_approver), can call this. A
    request whose requester has neither goes straight to HR/admin's final
    approve, exactly like before this chain existed."""
    manager_staff = await _resolve_own_staff(session, current_user)
    service = LeaveRequestService(session)
    request = await service.get_request(current_user.school_id, request_id)
    if not request:
        raise HTTPException(status_code=404, detail="Leave request not found")

    requester = await _resolve_staff_by_id(session, current_user, request.staff_id)
    if not await _is_authorized_approver(session, current_user.school_id, requester, manager_staff.id):
        raise HTTPException(status_code=403, detail="You are not this staff member's manager (or registered department head)")

    result = await service.manager_approve_request(current_user.school_id, request_id, current_user.id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])

    await log_event(
        session, actor=current_user, action="leave_request.manager_approved", entity_type="leave_request",
        entity_id=request_id, school_id=current_user.school_id,
        summary=f"{current_user.email} gave manager approval for a {request.leave_type} leave request from a direct report",
        old_values={"status": "pending"}, new_values={"status": "manager_approved"},
    )
    return result


@router.post("/leave-requests/{request_id}/manager-reject", response_model=dict)
async def manager_reject_leave_request(
    request_id: str,
    body: LeaveRequestReject,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    manager_staff = await _resolve_own_staff(session, current_user)
    service = LeaveRequestService(session)
    request = await service.get_request(current_user.school_id, request_id)
    if not request:
        raise HTTPException(status_code=404, detail="Leave request not found")

    requester = await _resolve_staff_by_id(session, current_user, request.staff_id)
    if not await _is_authorized_approver(session, current_user.school_id, requester, manager_staff.id):
        raise HTTPException(status_code=403, detail="You are not this staff member's manager (or registered department head)")

    result = await service.manager_reject_request(current_user.school_id, request_id, current_user.id, body.rejection_reason)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])

    await log_event(
        session, actor=current_user, action="leave_request.manager_rejected", entity_type="leave_request",
        entity_id=request_id, school_id=current_user.school_id,
        summary=f"{current_user.email} rejected a direct report's {request.leave_type} leave request",
        old_values={"status": "pending"}, new_values={"status": "rejected", "rejection_reason": body.rejection_reason},
    )
    return result


@router.post("/leave-requests/{request_id}/approve", response_model=dict)
async def approve_leave_request(
    request_id: str,
    current_user: User = Depends(require_permission("hr.leave_request.approve")),
    session: AsyncSession = Depends(get_session),
):
    service = LeaveRequestService(session)
    request = await service.get_request(current_user.school_id, request_id)
    if not request:
        raise HTTPException(status_code=404, detail="Leave request not found")

    # An HR/admin approver with no manager_id set on their own Staff row
    # would otherwise sail through this endpoint's only other guard (the
    # PENDING/manager-required check below only fires when the requester
    # HAS a manager) and self-approve their own leave. own_staff is looked
    # up directly rather than via _resolve_own_staff, since a legitimate
    # approver account may have no linked Staff row at all.
    own_staff_result = await session.execute(
        select(Staff).where(Staff.user_id == current_user.id, Staff.school_id == current_user.school_id)
    )
    own_staff = own_staff_result.scalar_one_or_none()
    if own_staff and own_staff.id == request.staff_id:
        raise HTTPException(status_code=403, detail="You cannot approve your own leave request — ask another admin to approve it")

    if request.status == LeaveRequestStatus.PENDING:
        requester = await _resolve_staff_by_id(session, current_user, request.staff_id)
        if requester.manager_id:
            raise HTTPException(status_code=400, detail="This request needs manager approval first")

    result = await service.approve_request(current_user.school_id, request_id, current_user.id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])

    await log_event(
        session, actor=current_user, action="leave_request.approved", entity_type="leave_request",
        entity_id=request_id, school_id=current_user.school_id,
        summary=f"{current_user.email} approved a {request.leave_type} leave request for staff {request.staff_id} ({request.days_requested} day(s))",
        old_values={"status": "pending"}, new_values={"status": "approved"},
    )
    return result


@router.post("/leave-requests/{request_id}/reject", response_model=dict)
async def reject_leave_request(
    request_id: str,
    body: LeaveRequestReject,
    current_user: User = Depends(require_permission("hr.leave_request.reject")),
    session: AsyncSession = Depends(get_session),
):
    service = LeaveRequestService(session)
    request = await service.get_request(current_user.school_id, request_id)
    if not request:
        raise HTTPException(status_code=404, detail="Leave request not found")

    result = await service.reject_request(current_user.school_id, request_id, current_user.id, body.rejection_reason)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])

    await log_event(
        session, actor=current_user, action="leave_request.rejected", entity_type="leave_request",
        entity_id=request_id, school_id=current_user.school_id,
        summary=f"{current_user.email} rejected a {request.leave_type} leave request for staff {request.staff_id}",
        old_values={"status": "pending"}, new_values={"status": "rejected", "rejection_reason": body.rejection_reason},
    )
    return result


@router.post("/leave-requests/{request_id}/cancel", response_model=dict)
async def cancel_leave_request(
    request_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    staff = await _resolve_own_staff(session, current_user)
    service = LeaveRequestService(session)
    result = await service.cancel_request(current_user.school_id, request_id, staff.id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/leave-requests/{request_id}/revoke", response_model=dict)
async def revoke_leave_request(
    request_id: str,
    body: LeaveRequestRevoke,
    current_user: User = Depends(require_permission("hr.leave_request.revoke")),
    session: AsyncSession = Depends(get_session),
):
    service = LeaveRequestService(session)
    request = await service.get_request(current_user.school_id, request_id)
    if not request:
        raise HTTPException(status_code=404, detail="Leave request not found")

    result = await service.revoke_request(current_user.school_id, request_id, current_user.id, body.revoke_reason)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])

    await log_event(
        session, actor=current_user, action="leave_request.revoked", entity_type="leave_request",
        entity_id=request_id, school_id=current_user.school_id,
        summary=f"{current_user.email} revoked an approved {request.leave_type} leave request for staff {request.staff_id}",
        old_values={"status": "approved"}, new_values={"status": "revoked", "revoke_reason": body.revoke_reason},
    )
    return result


# ── Leave balances ──────────────────────────────────────────────────────────

@router.get("/leave-balances/my", response_model=dict)
async def list_my_leave_balances(
    year: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    staff = await _resolve_own_staff(session, current_user)
    service = LeaveRequestService(session)
    balances = await service.list_balances(current_user.school_id, staff_id=staff.id, year=year)
    return {"records": [_serialize_balance(b) for b in balances]}


@router.get("/leave-balances", response_model=dict)
async def list_leave_balances(
    staff_id: Optional[str] = None,
    year: Optional[int] = None,
    current_user: User = Depends(require_permission("hr.leave_balance.view")),
    session: AsyncSession = Depends(get_session),
):
    if not current_user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    service = LeaveRequestService(session)
    balances = await service.list_balances(current_user.school_id, staff_id=staff_id, year=year)
    return {"records": [_serialize_balance(b) for b in balances]}


@router.post("/leave-balances/seed", response_model=dict)
async def seed_leave_balances(
    body: LeaveBalanceSeedRequest,
    current_user: User = Depends(require_permission("hr.leave_balance.manage")),
    session: AsyncSession = Depends(get_session),
):
    if not current_user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    service = LeaveRequestService(session)
    result = await service.seed_balances(current_user.school_id, body.year, body.staff_ids, current_user.id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.put("/leave-balances/{balance_id}", response_model=dict)
async def update_leave_balance(
    balance_id: str,
    body: LeaveBalanceUpdate,
    current_user: User = Depends(require_permission("hr.leave_balance.manage")),
    session: AsyncSession = Depends(get_session),
):
    if not current_user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    service = LeaveRequestService(session)
    result = await service.update_balance(current_user.school_id, balance_id, body.entitlement_days)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result
