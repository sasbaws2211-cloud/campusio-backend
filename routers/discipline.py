"""Discipline / Incident Tracking Router"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from sqlmodel import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
from typing import Optional, List

from models.discipline import (
    IncidentReport, IncidentReportCreate, IncidentReportUpdate,
    IncidentStudent, IncidentAction, IncidentActionCreate, RejectActionRequest,
    ActionApprovalStatus, DisciplineActionType, SUSPENSION_DEMERIT_THRESHOLD,
)
from models.student import Student
from models.user import User, UserRole
from database import get_session
from auth import get_current_user, require_roles
from dependencies import assert_campus_access

router = APIRouter(prefix="/discipline", tags=["Discipline"])

READ_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.TEACHER)
WRITE_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)


async def _serialize_incident(session: AsyncSession, incident: IncidentReport) -> dict:
    result = await session.execute(
        select(IncidentStudent).where(IncidentStudent.incident_id == incident.id)
    )
    students = result.scalars().all()
    return {
        **jsonable_encoder(incident),
        "students": [jsonable_encoder(s) for s in students],
    }


@router.get("/incidents", response_model=List[dict])
async def list_incidents(
    status: Optional[str] = None,
    student_id: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(require_roles(*READ_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """List incident reports, optionally filtered by status or an involved student"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(IncidentReport).where(IncidentReport.school_id == school_id)
    if status:
        query = query.where(IncidentReport.status == status)
    if student_id:
        subquery = select(IncidentStudent.incident_id).where(IncidentStudent.student_id == student_id)
        query = query.where(IncidentReport.id.in_(subquery))
    query = query.order_by(IncidentReport.created_at.desc()).offset(skip).limit(limit)

    result = await session.execute(query)
    incidents = result.scalars().all()
    return [await _serialize_incident(session, i) for i in incidents]


@router.post("/incidents", response_model=dict)
async def create_incident(
    data: IncidentReportCreate,
    current_user: User = Depends(require_roles(*READ_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Report a new incident, linking it to one or more students"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    if not data.student_ids:
        raise HTTPException(status_code=400, detail="At least one student must be linked to the incident")

    # IncidentStudent.student_id has no DB-level foreign key, so without this
    # a disciplinary incident (up to and including a SUSPENSION, which now
    # gates exam seating) could be attached to a student from a DIFFERENT
    # school given a guessable/leaked UUID -- same gap already closed
    # elsewhere for Grade/ExamComponentMark/Timetable references.
    found_students = (await session.execute(
        select(Student).where(Student.id.in_(data.student_ids), Student.school_id == school_id)
    )).scalars().all()
    if {s.id for s in found_students} != set(data.student_ids):
        raise HTTPException(status_code=400, detail="One or more student_ids do not exist for this school")
    # discipline.py previously had zero campus scoping anywhere -- a
    # campus-scoped SCHOOL_ADMIN/TEACHER could report an incident against a
    # student in a different campus of the same school.
    for student in found_students:
        assert_campus_access(current_user, student.campus_id)

    incident_fields = data.dict(exclude={"student_ids"})
    incident = IncidentReport(**incident_fields, school_id=school_id, reporter_staff_id=current_user.id)
    session.add(incident)
    await session.flush()

    for student_id in data.student_ids:
        session.add(IncidentStudent(incident_id=incident.id, student_id=student_id))

    await session.commit()
    await session.refresh(incident)
    return await _serialize_incident(session, incident)


@router.get("/incidents/{incident_id}", response_model=dict)
async def get_incident(
    incident_id: str,
    current_user: User = Depends(require_roles(*READ_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(IncidentReport).where(and_(IncidentReport.id == incident_id, IncidentReport.school_id == school_id))
    )
    incident = result.scalar_one_or_none()
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")

    return await _serialize_incident(session, incident)


@router.put("/incidents/{incident_id}", response_model=dict)
async def update_incident(
    incident_id: str,
    data: IncidentReportUpdate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(IncidentReport).where(and_(IncidentReport.id == incident_id, IncidentReport.school_id == school_id))
    )
    incident = result.scalar_one_or_none()
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")

    # IncidentReport.status is a free-text field (by convention "open"/
    # "resolved" only, per its own model comment) with no validation here --
    # any string could be set, silently breaking list_incidents' status
    # filter for that row from then on.
    if data.status is not None and data.status not in ("open", "resolved"):
        raise HTTPException(status_code=422, detail="status must be 'open' or 'resolved'")

    update_data = data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(incident, key, value)
    incident.updated_at = datetime.utcnow()

    session.add(incident)
    await session.commit()
    await session.refresh(incident)
    return await _serialize_incident(session, incident)


@router.delete("/incidents/{incident_id}", response_model=dict)
async def delete_incident(
    incident_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(IncidentReport).where(and_(IncidentReport.id == incident_id, IncidentReport.school_id == school_id))
    )
    incident = result.scalar_one_or_none()
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")

    await session.delete(incident)
    await session.commit()
    return {"message": "Incident deleted successfully", "id": incident_id}


@router.get("/incidents/{incident_id}/actions", response_model=List[dict])
async def list_incident_actions(
    incident_id: str,
    current_user: User = Depends(require_roles(*READ_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    incident_result = await session.execute(
        select(IncidentReport).where(and_(IncidentReport.id == incident_id, IncidentReport.school_id == school_id))
    )
    if not incident_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Incident not found")

    result = await session.execute(
        select(IncidentAction).where(IncidentAction.incident_id == incident_id).order_by(IncidentAction.created_at.desc())
    )
    return [jsonable_encoder(a) for a in result.scalars().all()]


@router.post("/incidents/{incident_id}/actions", response_model=dict)
async def create_incident_action(
    incident_id: str,
    data: IncidentActionCreate,
    current_user: User = Depends(require_roles(*READ_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Record a disciplinary action taken against a student for this incident.

    A SUSPENSION always starts PENDING and must be approved by a DIFFERENT
    SCHOOL_ADMIN/SUPER_ADMIN (see the /approve, /reject endpoints below)
    before it counts toward the demerit tally — including one created by an
    admin, not just a TEACHER (the person who sanctions a student shouldn't
    also be the sole sign-off on suspending their exam eligibility). Every
    other action type is APPROVED immediately."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    incident_result = await session.execute(
        select(IncidentReport).where(and_(IncidentReport.id == incident_id, IncidentReport.school_id == school_id))
    )
    if not incident_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Incident not found")

    # IncidentAction.student_id has no DB-level foreign key either -- same
    # cross-tenant gap as create_incident above, but for a single sanction
    # (including SUSPENSION) instead of the incident's student list.
    student_result = await session.execute(select(Student).where(Student.id == data.student_id, Student.school_id == school_id))
    student = student_result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    assert_campus_access(current_user, student.campus_id)

    # Neither the Pydantic model nor this endpoint validated demerit_points --
    # a negative value would silently reduce (or zero out) a student's
    # existing tally when summed by get_student_demerit_tally, defeating
    # SUSPENSION_DEMERIT_THRESHOLD tracking.
    if data.demerit_points < 0:
        raise HTTPException(status_code=422, detail="demerit_points cannot be negative")

    requires_approval = data.action_type == DisciplineActionType.SUSPENSION

    action = IncidentAction(
        **data.dict(),
        incident_id=incident_id,
        school_id=school_id,
        recorded_by=current_user.id,
        approval_status=ActionApprovalStatus.PENDING if requires_approval else ActionApprovalStatus.APPROVED,
        approved_by=None if requires_approval else current_user.id,
        approved_at=None if requires_approval else datetime.utcnow(),
    )
    session.add(action)
    await session.commit()
    await session.refresh(action)
    return jsonable_encoder(action)


@router.post("/actions/{action_id}/approve", response_model=dict)
async def approve_incident_action(
    action_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Approve a pending action (e.g. a suspension)"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(IncidentAction).where(and_(IncidentAction.id == action_id, IncidentAction.school_id == school_id))
    )
    action = result.scalar_one_or_none()
    if not action:
        raise HTTPException(status_code=404, detail="Action not found")
    if action.approval_status != ActionApprovalStatus.PENDING:
        raise HTTPException(status_code=409, detail=f"Action is already {action.approval_status.value}")
    if action.recorded_by == current_user.id:
        # requires_approval now covers every SUSPENSION, not just
        # teacher-created ones -- without this, the admin who created the
        # sanction (also in WRITE_ROLES) could immediately approve their own,
        # defeating the whole point of requiring a second sign-off.
        raise HTTPException(status_code=403, detail="You cannot approve a sanction you recorded yourself — ask another admin to review it")

    action.approval_status = ActionApprovalStatus.APPROVED
    action.approved_by = current_user.id
    action.approved_at = datetime.utcnow()

    session.add(action)
    await session.commit()
    await session.refresh(action)
    return jsonable_encoder(action)


@router.post("/actions/{action_id}/reject", response_model=dict)
async def reject_incident_action(
    action_id: str,
    data: RejectActionRequest,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Reject a pending action — it will never count toward the demerit tally"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(IncidentAction).where(and_(IncidentAction.id == action_id, IncidentAction.school_id == school_id))
    )
    action = result.scalar_one_or_none()
    if not action:
        raise HTTPException(status_code=404, detail="Action not found")
    if action.approval_status != ActionApprovalStatus.PENDING:
        raise HTTPException(status_code=409, detail=f"Action is already {action.approval_status.value}")
    if action.recorded_by == current_user.id:
        # Symmetric with approve_incident_action's block — same maker-checker
        # principle, matching routers/exam_papers.py's identical
        # approve/reject-your-own-work guard.
        raise HTTPException(status_code=403, detail="You cannot reject a sanction you recorded yourself — ask another admin to review it")

    action.approval_status = ActionApprovalStatus.REJECTED
    action.approved_by = current_user.id
    action.approved_at = datetime.utcnow()
    action.rejection_reason = data.rejection_reason

    session.add(action)
    await session.commit()
    await session.refresh(action)
    return jsonable_encoder(action)


@router.post("/actions/{action_id}/clear", response_model=dict)
async def clear_suspension(
    action_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Lift an APPROVED suspension's exam-eligibility restriction (see
    routers/exams.py::generate_seating) — suspensions have no built-in
    duration, so this is how a student becomes eligible again."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(IncidentAction).where(and_(IncidentAction.id == action_id, IncidentAction.school_id == school_id))
    )
    action = result.scalar_one_or_none()
    if not action:
        raise HTTPException(status_code=404, detail="Action not found")
    if action.action_type != DisciplineActionType.SUSPENSION:
        raise HTTPException(status_code=400, detail="Only a suspension action can be cleared")
    if action.approval_status != ActionApprovalStatus.APPROVED:
        raise HTTPException(status_code=400, detail=f"Cannot clear a suspension that is {action.approval_status.value}, not approved")
    if action.cleared:
        raise HTTPException(status_code=409, detail="This suspension is already cleared")

    action.cleared = True
    action.cleared_by = current_user.id
    action.cleared_at = datetime.utcnow()

    session.add(action)
    await session.commit()
    await session.refresh(action)
    return jsonable_encoder(action)


async def get_suspended_student_ids(session: AsyncSession, school_id: str, student_ids: List[str]) -> set:
    """Which of student_ids currently have an active (APPROVED, not yet
    cleared) SUSPENSION action — the exam-eligibility restriction
    routers/exams.py::generate_seating enforces. Batched (one query for
    however many students) rather than a per-student helper, since the only
    current caller checks a whole class roster at once."""
    if not student_ids:
        return set()
    result = await session.execute(
        select(IncidentAction.student_id).where(
            and_(
                IncidentAction.school_id == school_id,
                IncidentAction.student_id.in_(student_ids),
                IncidentAction.action_type == DisciplineActionType.SUSPENSION,
                IncidentAction.approval_status == ActionApprovalStatus.APPROVED,
                IncidentAction.cleared == False,  # noqa: E712
            )
        )
    )
    return set(result.scalars().all())


@router.get("/students/{student_id}/tally", response_model=dict)
async def get_student_demerit_tally(
    student_id: str,
    term_id: Optional[str] = None,
    current_user: User = Depends(require_roles(*READ_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Sum of demerit points for a student, optionally scoped to a term.
    Only APPROVED actions count — a pending (or rejected) teacher-created
    suspension doesn't affect the tally until an admin approves it."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = (
        select(func.coalesce(func.sum(IncidentAction.demerit_points), 0))
        .select_from(IncidentAction)
        .join(IncidentReport, IncidentAction.incident_id == IncidentReport.id)
        .where(
            and_(
                IncidentAction.student_id == student_id,
                IncidentAction.school_id == school_id,
                IncidentAction.approval_status == ActionApprovalStatus.APPROVED,
            )
        )
    )
    if term_id:
        query = query.where(IncidentReport.academic_term_id == term_id)

    result = await session.execute(query)
    total = result.scalar_one()

    return {
        "student_id": student_id,
        "term_id": term_id,
        "total_demerit_points": total,
        "at_suspension_threshold": total >= SUSPENSION_DEMERIT_THRESHOLD,
    }
