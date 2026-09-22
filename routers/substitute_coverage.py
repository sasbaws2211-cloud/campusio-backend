"""Substitute-teacher coverage for approved leave requests. Nested under
/leave-requests since coverage only makes sense in the context of a specific
approved request — see services/substitute_coverage_service.py.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from models.substitute_assignment import SubstituteAssignmentCreate, BulkSubstituteAssignmentCreate
from models.user import User, UserRole
from database import get_session
from auth import require_roles
from services.substitute_coverage_service import SubstituteCoverageService
from services.audit_service import log_event

router = APIRouter(tags=["Substitute Coverage"])

COVERAGE_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)


@router.get("/leave-requests/coverage/unassigned", response_model=dict)
async def get_unassigned_coverage(
    current_user: User = Depends(require_roles(*COVERAGE_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """School-wide dashboard view: every currently-relevant approved leave
    request that still has at least one uncovered period — previously an
    admin had to already know a specific leave_request_id to ever see this."""
    if not current_user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    service = SubstituteCoverageService(session)
    result = await service.get_unassigned_coverage_summary(current_user.school_id)
    return result


@router.get("/leave-requests/{request_id}/coverage", response_model=dict)
async def get_coverage_needs(
    request_id: str,
    current_user: User = Depends(require_roles(*COVERAGE_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    if not current_user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    service = SubstituteCoverageService(session)
    result = await service.get_coverage_needs(current_user.school_id, request_id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/leave-requests/{request_id}/coverage", response_model=dict)
async def assign_substitute(
    request_id: str,
    body: SubstituteAssignmentCreate,
    current_user: User = Depends(require_roles(*COVERAGE_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    if not current_user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    service = SubstituteCoverageService(session)
    result = await service.assign_substitute(
        current_user.school_id, request_id, body.timetable_entry_id, body.substitute_teacher_id, current_user.id,
    )
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])

    await log_event(
        session, actor=current_user, action="leave_request.substitute_assigned", entity_type="leave_request",
        entity_id=request_id, school_id=current_user.school_id,
        summary=f"{current_user.email} assigned a substitute for a period covered by leave request {request_id}",
        new_values={"timetable_entry_id": body.timetable_entry_id, "substitute_teacher_id": body.substitute_teacher_id},
    )
    return result


@router.post("/leave-requests/{request_id}/coverage/bulk", response_model=dict)
async def bulk_assign_substitute(
    request_id: str,
    body: BulkSubstituteAssignmentCreate,
    current_user: User = Depends(require_roles(*COVERAGE_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    if not current_user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    service = SubstituteCoverageService(session)
    result = await service.bulk_assign_substitute(
        current_user.school_id, request_id, body.substitute_teacher_id, current_user.id,
    )
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])

    await log_event(
        session, actor=current_user, action="leave_request.substitute_bulk_assigned", entity_type="leave_request",
        entity_id=request_id, school_id=current_user.school_id,
        summary=f"{current_user.email} bulk-assigned a substitute for {result['assigned']} period(s) covered by leave request {request_id}",
        new_values={"substitute_teacher_id": body.substitute_teacher_id, "assigned": result["assigned"]},
    )
    return result


@router.delete("/leave-requests/{request_id}/coverage/{timetable_entry_id}", response_model=dict)
async def remove_substitute_assignment(
    request_id: str,
    timetable_entry_id: str,
    current_user: User = Depends(require_roles(*COVERAGE_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    if not current_user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    service = SubstituteCoverageService(session)
    result = await service.remove_assignment(current_user.school_id, request_id, timetable_entry_id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result
