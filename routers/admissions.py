"""Admissions / Enrollment CRM Router"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from sqlmodel import select, and_
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
from typing import Optional, List
import secrets

from models.admissions import (
    Applicant, ApplicantCreate, ApplicantUpdate, ApplicantConvertRequest, ApplicationStatus, ApplicantDocument
)
from models.admissions_enterprise import ApplicantStageEvent
from models.student import Student, Parent, StudentParent
from models.classroom import Class
from models.school import AcademicTerm
from models.user import User, UserRole
from database import get_session
from auth import get_current_user, require_roles
from dependencies import resolve_write_campus_id, assert_campus_access
from routers.students import check_class_capacity, _record_enrollment

router = APIRouter(prefix="/admissions", tags=["Admissions"])

WRITE_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.REGISTRAR)


@router.get("/applicants", response_model=List[dict])
async def list_applicants(
    status: Optional[ApplicationStatus] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """List applicants, optionally filtered by pipeline status"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(Applicant).where(Applicant.school_id == school_id)
    if status:
        query = query.where(Applicant.status == status)
    query = query.order_by(Applicant.created_at.desc()).offset(skip).limit(limit)

    result = await session.execute(query)
    records = result.scalars().all()
    return [jsonable_encoder(r) for r in records]


async def _validate_class_and_term(session: AsyncSession, school_id: str, class_id: Optional[str], term_id: Optional[str]) -> None:
    """applying_for_class_id/applying_for_term_id must belong to the same
    school, or later cross-tenant leaks (routers/admissions_enterprise.py's
    analytics endpoint joins on these ids) and cross-tenant risk elsewhere
    become possible — previously neither was ever checked here."""
    if class_id:
        result = await session.execute(select(Class).where(Class.id == class_id, Class.school_id == school_id))
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="applying_for_class_id does not exist for this school")
    if term_id:
        result = await session.execute(select(AcademicTerm).where(AcademicTerm.id == term_id, AcademicTerm.school_id == school_id))
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="applying_for_term_id does not exist for this school")


@router.post("/applicants", response_model=dict)
async def create_applicant(
    applicant_data: ApplicantCreate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Register a new applicant (inquiry/application intake)"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    await _validate_class_and_term(session, school_id, applicant_data.applying_for_class_id, applicant_data.applying_for_term_id)

    applicant = Applicant(**applicant_data.dict(), school_id=school_id)
    session.add(applicant)
    await session.commit()
    await session.refresh(applicant)

    return jsonable_encoder(applicant)


@router.get("/applicants/{applicant_id}", response_model=dict)
async def get_applicant(
    applicant_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Get a single applicant"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(Applicant).where(and_(Applicant.id == applicant_id, Applicant.school_id == school_id))
    )
    applicant = result.scalar_one_or_none()
    if not applicant:
        raise HTTPException(status_code=404, detail="Applicant not found")

    return jsonable_encoder(applicant)


@router.put("/applicants/{applicant_id}", response_model=dict)
async def update_applicant(
    applicant_id: str,
    applicant_data: ApplicantUpdate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Update an applicant, including moving it through the pipeline status"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(Applicant).where(and_(Applicant.id == applicant_id, Applicant.school_id == school_id))
    )
    applicant = result.scalar_one_or_none()
    if not applicant:
        raise HTTPException(status_code=404, detail="Applicant not found")

    if applicant.converted_student_id:
        raise HTTPException(status_code=409, detail="Applicant has already been converted to a student")

    update_data = applicant_data.dict(exclude_unset=True)
    await _validate_class_and_term(
        session, school_id,
        update_data.get("applying_for_class_id"), update_data.get("applying_for_term_id"),
    )
    if "status" in update_data and update_data["status"] != applicant.status:
        next_status = update_data["status"]
        reason = update_data.get("rejection_reason") or update_data.get("withdrawal_reason")
        if next_status in (ApplicationStatus.REJECTED, ApplicationStatus.WITHDRAWN, ApplicationStatus.WAITLISTED) and not reason:
            raise HTTPException(status_code=422, detail="A reason is required for rejection, withdrawal, or waitlisting")
        if next_status == ApplicationStatus.WAITLISTED and not applicant.waitlist_rank:
            rank_result = await session.execute(select(func.count(Applicant.id)).where(Applicant.school_id == school_id, Applicant.status == ApplicationStatus.WAITLISTED))
            applicant.waitlist_rank = (rank_result.scalar() or 0) + 1
        session.add(ApplicantStageEvent(school_id=school_id, applicant_id=applicant.id, from_status=applicant.status.value, to_status=next_status.value, reason=reason, changed_by=current_user.id))
    for key, value in update_data.items():
        setattr(applicant, key, value)
    applicant.updated_at = datetime.utcnow()

    session.add(applicant)
    await session.commit()
    await session.refresh(applicant)

    return jsonable_encoder(applicant)


@router.delete("/applicants/{applicant_id}", response_model=dict)
async def delete_applicant(
    applicant_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Delete an applicant record"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(Applicant).where(and_(Applicant.id == applicant_id, Applicant.school_id == school_id))
    )
    applicant = result.scalar_one_or_none()
    if not applicant:
        raise HTTPException(status_code=404, detail="Applicant not found")

    await session.delete(applicant)
    await session.commit()

    return {"message": "Applicant deleted successfully", "id": applicant_id}


@router.get("/applicants/{applicant_id}/documents", response_model=List[dict])
async def list_applicant_documents(
    applicant_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """List documents the applicant/guardian uploaded on the public apply form."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    applicant_result = await session.execute(
        select(Applicant).where(and_(Applicant.id == applicant_id, Applicant.school_id == school_id))
    )
    if not applicant_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Applicant not found")

    result = await session.execute(
        select(ApplicantDocument)
        .where(ApplicantDocument.applicant_id == applicant_id)
        .order_by(ApplicantDocument.uploaded_at)
    )
    return [jsonable_encoder(d) for d in result.scalars().all()]


@router.post("/applicants/{applicant_id}/convert", response_model=dict)
async def convert_applicant(
    applicant_id: str,
    convert_data: ApplicantConvertRequest,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Convert an applicant into a real Student + Parent record.

    Lean-scope conversion: creates the core Student/Parent records only. It
    deliberately skips the student-portal-user auto-creation that
    routers/students.py's create_student does for directly-added students —
    an admin can wire that up afterward via the normal Students module once
    the applicant is enrolled. Class capacity and the StudentEnrollment
    audit-trail row are NOT skipped — they reuse create_student's own
    check_class_capacity/_record_enrollment helpers, so a converted
    applicant can't silently push a class over capacity or leave the class
    roster/history inconsistent with directly-added students.
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    # Locked FOR UPDATE: without it, two concurrent conversion requests
    # (a double-click, a retried request) could both read
    # converted_student_id as unset before either commits, and both create
    # a Student/Parent pair for the same applicant — one orphaned with no
    # Applicant link once the second commit overwrites converted_student_id.
    result = await session.execute(
        select(Applicant).where(and_(Applicant.id == applicant_id, Applicant.school_id == school_id)).with_for_update()
    )
    applicant = result.scalar_one_or_none()
    if not applicant:
        raise HTTPException(status_code=404, detail="Applicant not found")

    if applicant.converted_student_id:
        raise HTTPException(status_code=409, detail="Applicant has already been converted to a student")

    student_number = f"STU-{datetime.now().year}-{secrets.token_hex(3).upper()}"

    student = Student(
        school_id=school_id,
        student_id=student_number,
        first_name=applicant.first_name,
        last_name=applicant.last_name,
        other_names=applicant.other_names,
        date_of_birth=applicant.date_of_birth,
        gender=applicant.gender,
        admission_date=convert_data.admission_date,
        # Without this, a campus-scoped registrar who converts an applicant
        # gets locked out of the very student they just created — Student
        # rows with campus_id=None are off-limits to a scoped user
        # (dependencies.py::assert_campus_access). create_student
        # (routers/students.py) already does this; convert_applicant hadn't.
        campus_id=resolve_write_campus_id(current_user, convert_data.campus_id),
    )
    session.add(student)
    await session.flush()

    # A campus-scoped registrar can't convert an applicant into a class
    # belonging to a different campus — check_class_capacity only scopes by
    # school_id, so this check was previously entirely missing from the
    # conversion path (contrast with assign_fee_to_class/create_student,
    # which already enforce this for their own respective actions).
    class_result = await session.execute(select(Class).where(Class.id == convert_data.class_id, Class.school_id == school_id))
    target_class = class_result.scalar_one_or_none()
    if not target_class:
        raise HTTPException(status_code=404, detail="Class not found")
    assert_campus_access(current_user, target_class.campus_id)

    waitlist_entry = await check_class_capacity(session, school_id, convert_data.class_id, auto_waitlist=True, student_id=student.id)
    if waitlist_entry is None:
        student.class_id = convert_data.class_id
        session.add(student)
        await _record_enrollment(session, school_id, student.id, convert_data.class_id, reason="admitted")

    guardian_name_parts = applicant.guardian_name.split(" ", 1)
    parent = Parent(
        school_id=school_id,
        first_name=guardian_name_parts[0],
        last_name=guardian_name_parts[1] if len(guardian_name_parts) > 1 else "",
        relationship=applicant.guardian_relationship,
        phone=applicant.guardian_phone,
        email=applicant.guardian_email,
        is_emergency_contact=True,
    )
    session.add(parent)
    await session.flush()

    link = StudentParent(student_id=student.id, parent_id=parent.id)
    session.add(link)

    applicant.status = ApplicationStatus.ENROLLED
    applicant.converted_student_id = student.id
    applicant.updated_at = datetime.utcnow()
    session.add(applicant)

    await session.commit()
    await session.refresh(student)

    response = {
        "applicant_id": applicant.id,
        "student_id": student.id,
        "student_number": student.student_id,
        "parent_id": parent.id,
        "message": "Applicant converted to student successfully",
    }
    # Additive — the student record is always created; these two fields just
    # flag that the requested class was full and the student was queued on
    # the class waitlist instead of enrolled directly (same convention as
    # routers/students.py::create_student's own waitlisted/position fields).
    if waitlist_entry:
        response["waitlisted"] = True
        response["position"] = waitlist_entry.position
    return response
