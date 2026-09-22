"""Restricted student welfare and intervention case workflow."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_roles
from database import get_session
from models.student import Student
from models.student_support import StudentSupportCase, StudentSupportCaseCreate, StudentSupportCaseUpdate, SupportCaseStatus, is_safeguarding_case
from models.user import User, UserRole
from services.audit_service import log_event

router = APIRouter(prefix="/student-support", tags=["Student Support"])
ACCESS_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR, UserRole.NURSE, UserRole.SAFEGUARDING_LEAD)
# Child-protection cases (case_type == "safeguarding") get a narrower gate
# than ordinary welfare/SEN/counselling cases — HR and the clinic nurse are
# in ACCESS_ROLES for the module generally, but neither is safeguarding-
# cleared by default, so they never see this specific case_type.
SAFEGUARDING_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.SAFEGUARDING_LEAD)


def _school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


def _require_safeguarding_clearance(user: User, case: StudentSupportCase) -> None:
    if is_safeguarding_case(case.case_type) and user.role not in SAFEGUARDING_ROLES:
        raise HTTPException(status_code=403, detail="This case is restricted to safeguarding-cleared staff")


def _case_dict(case: StudentSupportCase, student: Student) -> dict:
    return {"id": case.id, "student_id": case.student_id, "student_name": f"{student.first_name} {student.last_name}", "case_type": case.case_type, "severity": case.severity.value, "status": case.status.value, "summary": case.summary, "action_plan": case.action_plan, "next_review_date": case.next_review_date, "assigned_to": case.assigned_to, "created_by": case.created_by, "resolved_at": case.resolved_at.isoformat() if case.resolved_at else None, "created_at": case.created_at.isoformat()}


async def _find_case(case_id: str, user: User, session: AsyncSession):
    result = await session.execute(select(StudentSupportCase, Student).join(Student, Student.id == StudentSupportCase.student_id).where(StudentSupportCase.id == case_id, StudentSupportCase.school_id == _school_id(user)))
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Support case not found")
    return row[0], row[1]


@router.get("/cases", response_model=list[dict])
async def list_cases(student_id: str | None = None, status: SupportCaseStatus | None = Query(None), current_user: User = Depends(require_roles(*ACCESS_ROLES)), session: AsyncSession = Depends(get_session)):
    query = select(StudentSupportCase, Student).join(Student, Student.id == StudentSupportCase.student_id).where(StudentSupportCase.school_id == _school_id(current_user))
    if student_id:
        query = query.where(StudentSupportCase.student_id == student_id)
    if status:
        query = query.where(StudentSupportCase.status == status)
    result = await session.execute(query.order_by(StudentSupportCase.created_at.desc()))
    can_see_safeguarding = current_user.role in SAFEGUARDING_ROLES
    return [_case_dict(case, student) for case, student in result.all() if can_see_safeguarding or not is_safeguarding_case(case.case_type)]


@router.post("/cases", response_model=dict)
async def create_case(payload: StudentSupportCaseCreate, current_user: User = Depends(require_roles(*ACCESS_ROLES)), session: AsyncSession = Depends(get_session)):
    if is_safeguarding_case(payload.case_type) and current_user.role not in SAFEGUARDING_ROLES:
        raise HTTPException(status_code=403, detail="Only safeguarding-cleared staff can open a safeguarding case")
    school_id = _school_id(current_user)
    student = (await session.execute(select(Student).where(Student.id == payload.student_id, Student.school_id == school_id))).scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=400, detail="Student not found in this school")
    case = StudentSupportCase(school_id=school_id, created_by=current_user.id, **payload.model_dump())
    session.add(case)
    await session.commit()
    await session.refresh(case)
    await log_event(session, current_user, "student_support.case.created", "support_case", case.id, "Created restricted student support case", school_id=school_id)
    return _case_dict(case, student)


@router.put("/cases/{case_id}", response_model=dict)
async def update_case(case_id: str, payload: StudentSupportCaseUpdate, current_user: User = Depends(require_roles(*ACCESS_ROLES)), session: AsyncSession = Depends(get_session)):
    case, student = await _find_case(case_id, current_user, session)
    _require_safeguarding_clearance(current_user, case)
    if payload.case_type is not None and is_safeguarding_case(payload.case_type) and current_user.role not in SAFEGUARDING_ROLES:
        raise HTTPException(status_code=403, detail="Only safeguarding-cleared staff can mark a case as safeguarding")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(case, key, value)
    if payload.status in (SupportCaseStatus.RESOLVED, SupportCaseStatus.CLOSED):
        case.resolved_at = case.resolved_at or datetime.utcnow()
    elif payload.status in (SupportCaseStatus.OPEN, SupportCaseStatus.IN_PROGRESS):
        case.resolved_at = None
    case.updated_at = datetime.utcnow()
    session.add(case)
    await session.commit()
    await log_event(session, current_user, "student_support.case.updated", "support_case", case.id, "Updated restricted student support case", school_id=current_user.school_id)
    return _case_dict(case, student)


@router.delete("/cases/{case_id}", response_model=dict)
async def delete_case(case_id: str, current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)), session: AsyncSession = Depends(get_session)):
    case, _ = await _find_case(case_id, current_user, session)
    _require_safeguarding_clearance(current_user, case)
    case.status = SupportCaseStatus.CLOSED
    case.updated_at = datetime.utcnow()
    case.action_plan = f"{case.action_plan or ''}\nRecord retained; deletion request by {current_user.id}."
    session.add(case)
    await session.commit()
    await log_event(session, current_user, "student_support.case.archived", "support_case", case.id, "Archived restricted student support case", school_id=current_user.school_id)
    return {"message": "Support case archived", "id": case.id}