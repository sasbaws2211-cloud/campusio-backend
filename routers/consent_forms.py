"""General-purpose consent forms / digital permission slips — see
models/parent_consent_forms.py for why this is deliberately separate from
the safeguarding-only ParentConsent under /student-support/secure."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user, require_roles
from database import get_session
from models.classroom import Class
from models.parent_consent_forms import (
    ConsentForm, ConsentFormCreate, ConsentResponse, ConsentResponseSubmit, ConsentResponseStatus,
)
from models.student import Student
from models.user import User, UserRole
from routers.parent import get_parent_children_ids

router = APIRouter(prefix="/consent-forms", tags=["Consent Forms"])

STAFF_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.TEACHER, UserRole.REGISTRAR)


def _school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


def _form_to_dict(form: ConsentForm) -> dict:
    return {
        "id": form.id,
        "title": form.title,
        "description": form.description,
        "consent_type": form.consent_type,
        "target_class_id": form.target_class_id,
        "respond_by": form.respond_by,
        "is_active": form.is_active,
        "created_by": form.created_by,
        "created_at": form.created_at,
    }


def _response_to_dict(response: ConsentResponse, form: Optional[ConsentForm] = None, student: Optional[Student] = None) -> dict:
    return {
        "id": response.id,
        "consent_form_id": response.consent_form_id,
        "form_title": form.title if form else None,
        "form_description": form.description if form else None,
        "respond_by": form.respond_by if form else None,
        "student_id": response.student_id,
        "student_name": f"{student.first_name} {student.last_name}" if student else None,
        "parent_user_id": response.parent_user_id,
        "status": response.status,
        "notes": response.notes,
        "responded_at": response.responded_at,
        "created_at": response.created_at,
    }


@router.get("", response_model=List[dict])
async def list_consent_forms(
    current_user: User = Depends(require_roles(*STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    result = await session.execute(select(ConsentForm).where(ConsentForm.school_id == school_id).order_by(ConsentForm.created_at.desc()))
    forms = result.scalars().all()

    counts = {}
    if forms:
        rows = (
            await session.execute(
                select(ConsentResponse.consent_form_id, ConsentResponse.status, func.count(ConsentResponse.id))
                .where(ConsentResponse.consent_form_id.in_([f.id for f in forms]))
                .group_by(ConsentResponse.consent_form_id, ConsentResponse.status)
            )
        ).all()
        for form_id, status, count in rows:
            counts.setdefault(form_id, {}).setdefault(status, count)

    return [{**_form_to_dict(f), "response_counts": counts.get(f.id, {})} for f in forms]


@router.post("", response_model=dict)
async def create_consent_form(
    payload: ConsentFormCreate,
    current_user: User = Depends(require_roles(*STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    target_student_ids: List[str] = []

    if payload.target_class_id:
        cls = (await session.execute(select(Class).where(Class.id == payload.target_class_id, Class.school_id == school_id))).scalar_one_or_none()
        if not cls:
            raise HTTPException(status_code=400, detail="Class not found in this school")
        target_student_ids.extend(
            (await session.execute(select(Student.id).where(Student.class_id == payload.target_class_id, Student.school_id == school_id))).scalars().all()
        )
    if payload.target_student_ids:
        valid_ids = (
            await session.execute(select(Student.id).where(Student.id.in_(payload.target_student_ids), Student.school_id == school_id))
        ).scalars().all()
        target_student_ids.extend(valid_ids)

    target_student_ids = list(set(target_student_ids))
    if not target_student_ids:
        raise HTTPException(status_code=422, detail="target_class_id or target_student_ids must resolve to at least one student")

    form = ConsentForm(
        school_id=school_id, created_by=current_user.id,
        title=payload.title, description=payload.description, consent_type=payload.consent_type,
        target_class_id=payload.target_class_id, respond_by=payload.respond_by,
    )
    session.add(form)
    await session.flush()

    for student_id in target_student_ids:
        session.add(ConsentResponse(school_id=school_id, consent_form_id=form.id, student_id=student_id))

    await session.commit()
    await session.refresh(form)
    return {**_form_to_dict(form), "students_targeted": len(target_student_ids)}


@router.get("/{form_id}/responses", response_model=List[dict])
async def list_consent_form_responses(
    form_id: str,
    current_user: User = Depends(require_roles(*STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    form = (await session.execute(select(ConsentForm).where(ConsentForm.id == form_id, ConsentForm.school_id == school_id))).scalar_one_or_none()
    if not form:
        raise HTTPException(status_code=404, detail="Consent form not found")
    result = await session.execute(
        select(ConsentResponse, Student).join(Student, Student.id == ConsentResponse.student_id).where(ConsentResponse.consent_form_id == form_id)
    )
    return [_response_to_dict(response, form, student) for response, student in result.all()]


@router.get("/my-responses", response_model=List[dict])
async def list_my_consent_responses(
    status_filter: Optional[str] = None,
    current_user: User = Depends(require_roles(UserRole.PARENT)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    children = await get_parent_children_ids(current_user, session)
    if not children:
        return []
    stmt = (
        select(ConsentResponse, ConsentForm, Student)
        .join(ConsentForm, ConsentForm.id == ConsentResponse.consent_form_id)
        .join(Student, Student.id == ConsentResponse.student_id)
        .where(ConsentResponse.school_id == school_id, ConsentResponse.student_id.in_(children))
    )
    if status_filter:
        stmt = stmt.where(ConsentResponse.status == status_filter)
    result = await session.execute(stmt.order_by(ConsentResponse.created_at.desc()))
    return [_response_to_dict(response, form, student) for response, form, student in result.all()]


@router.post("/responses/{response_id}", response_model=dict)
async def submit_consent_response(
    response_id: str,
    payload: ConsentResponseSubmit,
    current_user: User = Depends(require_roles(UserRole.PARENT)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    children = await get_parent_children_ids(current_user, session)
    result = await session.execute(
        select(ConsentResponse, ConsentForm, Student)
        .join(ConsentForm, ConsentForm.id == ConsentResponse.consent_form_id)
        .join(Student, Student.id == ConsentResponse.student_id)
        .where(ConsentResponse.id == response_id, ConsentResponse.school_id == school_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Consent response not found")
    response, form, student = row
    if response.student_id not in children:
        raise HTTPException(status_code=403, detail="Not authorized for this student")
    if payload.status == ConsentResponseStatus.PENDING:
        raise HTTPException(status_code=422, detail="A response must grant or decline consent")

    response.status = payload.status.value
    response.notes = payload.notes
    response.parent_user_id = current_user.id
    response.responded_at = datetime.utcnow()
    await session.commit()
    await session.refresh(response)
    return _response_to_dict(response, form, student)
