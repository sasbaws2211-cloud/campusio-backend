"""Certificate Generation Router"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from sqlmodel import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
from typing import Optional, List
import secrets

from models.certificates import (
    CertificateIssuance, CertificateIssuanceCreate, CertificateType,
    CertificateTemplate, CertificateTemplateCreate, CertificateTemplateUpdate,
)
from models.student import Student, StudentStatus
from models.classroom import Class
from models.user import User, UserRole
from database import get_session
from auth import get_current_user, require_roles
from services.certificate_pdf_service import CertificatePDFService

router = APIRouter(prefix="/certificates", tags=["Certificates"])

WRITE_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)

TEMPLATE_BY_TYPE = {
    CertificateType.LEAVING: "certificate_leaving.html",
    CertificateType.TRANSFER: "certificate_transfer.html",
    CertificateType.ACHIEVEMENT: "certificate_achievement.html",
}


async def _get_custom_template_html(session: AsyncSession, school_id: str, certificate_type: CertificateType) -> Optional[str]:
    """The most recently updated is_default template for this school/type, if any."""
    result = await session.execute(
        select(CertificateTemplate).where(
            and_(
                CertificateTemplate.school_id == school_id,
                CertificateTemplate.certificate_type == certificate_type,
                CertificateTemplate.is_default == True,  # noqa: E712
            )
        ).order_by(CertificateTemplate.updated_at.desc())
    )
    template = result.scalars().first()
    return template.html_content if template else None


async def _build_certificate_data(session: AsyncSession, school_id: str, student: Student, issuance: CertificateIssuance) -> dict:
    from models.school import School
    school_result = await session.execute(select(School).where(School.id == school_id))
    school = school_result.scalar_one_or_none()

    last_class_completed = None
    if student.class_id:
        class_result = await session.execute(select(Class).where(Class.id == student.class_id))
        cls = class_result.scalar_one_or_none()
        last_class_completed = cls.name if cls else None

    return {
        "school_name": school.name if school else "School",
        "student_name": f"{student.first_name} {student.last_name}",
        "student_id_code": student.student_id,
        "date_of_birth": student.date_of_birth,
        "last_class_completed": last_class_completed,
        "exit_date": student.exit_date,
        "exit_reason": student.exit_reason,
        "transfer_destination_school": student.transfer_destination_school,
        "certificate_number": issuance.certificate_number,
        "issue_date": issuance.issue_date,
        "remarks": issuance.remarks,
    }


@router.post("/{student_id}/generate")
async def generate_certificate(
    student_id: str,
    type: CertificateType = Query(..., description="Certificate type"),
    data: CertificateIssuanceCreate = CertificateIssuanceCreate(),
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Generate a certificate for a student, recording an issuance and streaming the PDF"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    student_result = await session.execute(
        select(Student).where(and_(Student.id == student_id, Student.school_id == school_id))
    )
    student = student_result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    if type in (CertificateType.LEAVING, CertificateType.TRANSFER) and student.status == StudentStatus.ACTIVE:
        raise HTTPException(
            status_code=400,
            detail="Complete the student's exit (Exit Student action) before issuing a leaving or transfer certificate."
        )

    certificate_number = f"{school_id[:8]}-{type.value[:3].upper()}-{secrets.token_hex(4).upper()}"

    issuance = CertificateIssuance(
        school_id=school_id,
        student_id=student_id,
        certificate_type=type,
        certificate_number=certificate_number,
        issue_date=datetime.utcnow().strftime("%Y-%m-%d"),
        issued_by=current_user.id,
        remarks=data.remarks,
    )
    session.add(issuance)
    await session.commit()
    await session.refresh(issuance)

    pdf_service = CertificatePDFService()
    cert_data = await _build_certificate_data(session, school_id, student, issuance)
    cert_data["qr_image_data_uri"] = pdf_service.generate_qr_data_uri(issuance.certificate_number)
    custom_html = await _get_custom_template_html(session, school_id, type)

    try:
        pdf_bytes = pdf_service.generate_pdf(TEMPLATE_BY_TYPE[type], cert_data, custom_html=custom_html)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate certificate PDF: {str(e)}")

    safe_name = cert_data["student_name"].replace(" ", "_")
    filename = f"certificate_{type.value}_{safe_name}.pdf"

    return StreamingResponse(
        iter([pdf_bytes]),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@router.get("/issuances", response_model=List[dict])
async def list_issuances(
    student_id: Optional[str] = None,
    certificate_type: Optional[CertificateType] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(CertificateIssuance).where(CertificateIssuance.school_id == school_id)
    if student_id:
        query = query.where(CertificateIssuance.student_id == student_id)
    if certificate_type:
        query = query.where(CertificateIssuance.certificate_type == certificate_type)
    query = query.order_by(CertificateIssuance.created_at.desc()).offset(skip).limit(limit)

    result = await session.execute(query)
    return [jsonable_encoder(i) for i in result.scalars().all()]


@router.get("/verify/{certificate_number}", response_model=dict)
async def verify_certificate(
    certificate_number: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES, UserRole.SECURITY_OFFICER)),
    session: AsyncSession = Depends(get_session)
):
    """Gate lookup: resolve a certificate number to student/issuance details."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(CertificateIssuance).where(
            and_(CertificateIssuance.certificate_number == certificate_number, CertificateIssuance.school_id == school_id)
        )
    )
    issuance = result.scalar_one_or_none()
    if not issuance:
        raise HTTPException(status_code=404, detail="Certificate not found")

    student_result = await session.execute(
        select(Student).where(and_(Student.id == issuance.student_id, Student.school_id == school_id))
    )
    student = student_result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Certificate holder not found")

    return {
        "certificate_number": issuance.certificate_number,
        "certificate_type": issuance.certificate_type.value,
        "student_name": f"{student.first_name} {student.last_name}",
        "issue_date": issuance.issue_date,
        "issued_by": issuance.issued_by,
        "is_valid": True,
    }


@router.get("/issuances/{issuance_id}/reprint")
async def reprint_certificate(
    issuance_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Regenerate the PDF for an existing issuance without creating a new record"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(CertificateIssuance).where(
            and_(CertificateIssuance.id == issuance_id, CertificateIssuance.school_id == school_id)
        )
    )
    issuance = result.scalar_one_or_none()
    if not issuance:
        raise HTTPException(status_code=404, detail="Certificate issuance not found")

    student_result = await session.execute(
        select(Student).where(and_(Student.id == issuance.student_id, Student.school_id == school_id))
    )
    student = student_result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    pdf_service = CertificatePDFService()
    cert_data = await _build_certificate_data(session, school_id, student, issuance)
    cert_data["qr_image_data_uri"] = pdf_service.generate_qr_data_uri(issuance.certificate_number)
    custom_html = await _get_custom_template_html(session, school_id, issuance.certificate_type)

    try:
        pdf_bytes = pdf_service.generate_pdf(
            TEMPLATE_BY_TYPE[issuance.certificate_type], cert_data, custom_html=custom_html
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate certificate PDF: {str(e)}")

    safe_name = cert_data["student_name"].replace(" ", "_")
    filename = f"certificate_{issuance.certificate_type.value}_{safe_name}_reprint.pdf"

    return StreamingResponse(
        iter([pdf_bytes]),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


# ============================================================================
# CERTIFICATE TEMPLATES
# ============================================================================

@router.get("/templates", response_model=List[dict])
async def list_certificate_templates(
    certificate_type: Optional[CertificateType] = None,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(CertificateTemplate).where(CertificateTemplate.school_id == school_id)
    if certificate_type:
        query = query.where(CertificateTemplate.certificate_type == certificate_type)
    query = query.order_by(CertificateTemplate.updated_at.desc())

    result = await session.execute(query)
    return [jsonable_encoder(t) for t in result.scalars().all()]


@router.post("/templates", response_model=dict)
async def create_certificate_template(
    data: CertificateTemplateCreate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    template = CertificateTemplate(**data.dict(), school_id=school_id)
    session.add(template)
    await session.commit()
    await session.refresh(template)
    return jsonable_encoder(template)


@router.put("/templates/{template_id}", response_model=dict)
async def update_certificate_template(
    template_id: str,
    data: CertificateTemplateUpdate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(CertificateTemplate).where(
            and_(CertificateTemplate.id == template_id, CertificateTemplate.school_id == school_id)
        )
    )
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Certificate template not found")

    update_data = data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(template, key, value)
    template.updated_at = datetime.utcnow()

    session.add(template)
    await session.commit()
    await session.refresh(template)
    return jsonable_encoder(template)


@router.delete("/templates/{template_id}", response_model=dict)
async def delete_certificate_template(
    template_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(CertificateTemplate).where(
            and_(CertificateTemplate.id == template_id, CertificateTemplate.school_id == school_id)
        )
    )
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Certificate template not found")

    await session.delete(template)
    await session.commit()
    return {"message": "Certificate template deleted successfully", "id": template_id}
