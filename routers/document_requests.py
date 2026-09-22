"""Parent/student document request workflow — transfer/leaving certificates,
transcripts, letters. Certificate-backed types are fulfilled by linking the
CertificateIssuance the existing /certificates/{id}/generate endpoint
produces (that endpoint's own rule that a student must already be exited
before a transfer/leaving certificate can be generated is untouched —
requesting one here doesn't bypass it, an admin still exits the student and
generates normally, then links the result back to this request)."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user, require_roles
from database import get_session
from models.certificates import CertificateIssuance
from models.parent_requests import (
    DocumentRequest, DocumentRequestCreate, DocumentRequestReview, DocumentRequestFulfill, DocumentRequestStatus,
)
from models.student import Student
from models.user import User, UserRole
from routers.parent import get_parent_children_ids, verify_child_access
from services.transcript_service import build_transcript_pdf

router = APIRouter(prefix="/document-requests", tags=["Document Requests"])

STAFF_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.REGISTRAR)
REQUESTER_ROLES = (UserRole.PARENT, UserRole.STUDENT)


def _school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


def _to_dict(item: DocumentRequest, student: Optional[Student] = None) -> dict:
    return {
        "id": item.id,
        "student_id": item.student_id,
        "student_name": f"{student.first_name} {student.last_name}" if student else None,
        "requested_by": item.requested_by,
        "document_type": item.document_type,
        "reason": item.reason,
        "status": item.status,
        "reviewed_by": item.reviewed_by,
        "review_notes": item.review_notes,
        "certificate_issuance_id": item.certificate_issuance_id,
        "fulfillment_file_url": item.fulfillment_file_url,
        "completed_at": item.completed_at,
        "created_at": item.created_at,
    }


async def _verify_requester_access(student_id: str, current_user: User, session: AsyncSession) -> Student:
    if current_user.role == UserRole.PARENT:
        return await verify_child_access(student_id, current_user, session)
    # STUDENT — only their own record.
    result = await session.execute(select(Student).where(Student.id == student_id, Student.user_id == current_user.id))
    student = result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found or access denied")
    return student


@router.get("", response_model=List[dict])
async def list_document_requests(
    status_filter: Optional[str] = None,
    student_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    stmt = select(DocumentRequest, Student).join(Student, Student.id == DocumentRequest.student_id).where(DocumentRequest.school_id == school_id)

    if current_user.role == UserRole.PARENT:
        children = await get_parent_children_ids(current_user, session)
        stmt = stmt.where(DocumentRequest.student_id.in_(children))
    elif current_user.role == UserRole.STUDENT:
        stmt = stmt.where(DocumentRequest.requested_by == current_user.id)
    elif current_user.role not in STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Access denied")

    if status_filter:
        stmt = stmt.where(DocumentRequest.status == status_filter)
    if student_id:
        stmt = stmt.where(DocumentRequest.student_id == student_id)

    result = await session.execute(stmt.order_by(DocumentRequest.created_at.desc()))
    return [_to_dict(item, student) for item, student in result.all()]


@router.post("", response_model=dict)
async def create_document_request(
    payload: DocumentRequestCreate,
    current_user: User = Depends(require_roles(*REQUESTER_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    student = await _verify_requester_access(payload.student_id, current_user, session)
    item = DocumentRequest(
        school_id=school_id, requested_by=current_user.id,
        student_id=payload.student_id, document_type=payload.document_type.value, reason=payload.reason,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _to_dict(item, student)


@router.post("/{request_id}/cancel", response_model=dict)
async def cancel_document_request(
    request_id: str,
    current_user: User = Depends(require_roles(*REQUESTER_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    item = (await session.execute(select(DocumentRequest).where(DocumentRequest.id == request_id, DocumentRequest.school_id == school_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Document request not found")
    if item.requested_by != current_user.id:
        raise HTTPException(status_code=403, detail="You can only cancel your own requests")
    if item.status in (DocumentRequestStatus.COMPLETED.value, DocumentRequestStatus.CANCELLED.value, DocumentRequestStatus.REJECTED.value):
        raise HTTPException(status_code=400, detail=f"Cannot cancel a {item.status} request")
    item.status = DocumentRequestStatus.CANCELLED.value
    item.updated_at = datetime.utcnow()
    await session.commit()
    return {"id": item.id, "status": item.status}


@router.post("/{request_id}/review", response_model=dict)
async def review_document_request(
    request_id: str,
    payload: DocumentRequestReview,
    current_user: User = Depends(require_roles(*STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    result = await session.execute(
        select(DocumentRequest, Student).join(Student, Student.id == DocumentRequest.student_id).where(DocumentRequest.id == request_id, DocumentRequest.school_id == school_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Document request not found")
    item, student = row
    if item.status in (DocumentRequestStatus.COMPLETED.value, DocumentRequestStatus.REJECTED.value, DocumentRequestStatus.CANCELLED.value):
        raise HTTPException(status_code=400, detail=f"Cannot review a {item.status} request")
    if payload.status == DocumentRequestStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Use the fulfill endpoint to mark a request completed — it requires an actual certificate_issuance_id or fulfillment_file_url")
    if payload.status == DocumentRequestStatus.CANCELLED:
        raise HTTPException(status_code=400, detail="Only the requester can cancel their own request")
    item.status = payload.status.value
    item.review_notes = payload.review_notes
    item.reviewed_by = current_user.id
    item.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(item)
    return _to_dict(item, student)


@router.post("/{request_id}/fulfill", response_model=dict)
async def fulfill_document_request(
    request_id: str,
    payload: DocumentRequestFulfill,
    current_user: User = Depends(require_roles(*STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """Marks a document request completed, attached to whichever it was
    fulfilled with — a linked CertificateIssuance (already generated via
    the normal /certificates/{id}/generate flow) or a directly-uploaded
    file URL for non-certificate document types."""
    school_id = _school_id(current_user)
    result = await session.execute(
        select(DocumentRequest, Student).join(Student, Student.id == DocumentRequest.student_id).where(DocumentRequest.id == request_id, DocumentRequest.school_id == school_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Document request not found")
    item, student = row
    if item.status not in (DocumentRequestStatus.PENDING.value, DocumentRequestStatus.IN_PROGRESS.value, DocumentRequestStatus.READY.value):
        raise HTTPException(status_code=400, detail=f"Cannot fulfill a {item.status} request")
    if not payload.certificate_issuance_id and not payload.fulfillment_file_url:
        raise HTTPException(status_code=422, detail="Provide either a certificate_issuance_id or a fulfillment_file_url")

    if payload.certificate_issuance_id:
        issuance = (
            await session.execute(select(CertificateIssuance).where(CertificateIssuance.id == payload.certificate_issuance_id, CertificateIssuance.school_id == school_id, CertificateIssuance.student_id == item.student_id))
        ).scalar_one_or_none()
        if not issuance:
            raise HTTPException(status_code=400, detail="Certificate issuance not found for this student")
        item.certificate_issuance_id = issuance.id

    if payload.fulfillment_file_url:
        item.fulfillment_file_url = payload.fulfillment_file_url

    item.status = DocumentRequestStatus.COMPLETED.value
    item.completed_at = datetime.utcnow()
    item.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(item)
    return _to_dict(item, student)


@router.post("/{request_id}/generate-transcript", response_model=dict)
async def generate_transcript(
    request_id: str,
    current_user: User = Depends(require_roles(*STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """Fulfils a document_type='transcript' request by pointing it at the
    live-regenerated PDF endpoint below, instead of requiring a manual file
    upload — no PDF is stored, it's compiled fresh from approved report
    cards on every download (see services/transcript_service.py)."""
    school_id = _school_id(current_user)
    result = await session.execute(
        select(DocumentRequest, Student).join(Student, Student.id == DocumentRequest.student_id).where(DocumentRequest.id == request_id, DocumentRequest.school_id == school_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Document request not found")
    item, student = row
    if item.document_type != "transcript":
        raise HTTPException(status_code=400, detail="This request is not a transcript request")
    if item.status not in (DocumentRequestStatus.PENDING.value, DocumentRequestStatus.IN_PROGRESS.value, DocumentRequestStatus.READY.value):
        raise HTTPException(status_code=400, detail=f"Cannot fulfill a {item.status} request")

    pdf_bytes = await build_transcript_pdf(session, student)
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="This student has no approved report cards to compile a transcript from")

    item.fulfillment_file_url = f"/api/document-requests/{item.id}/transcript.pdf"
    item.status = DocumentRequestStatus.COMPLETED.value
    item.completed_at = datetime.utcnow()
    item.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(item)
    return _to_dict(item, student)


@router.get("/{request_id}/transcript.pdf")
async def download_transcript(
    request_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    result = await session.execute(
        select(DocumentRequest, Student).join(Student, Student.id == DocumentRequest.student_id).where(DocumentRequest.id == request_id, DocumentRequest.school_id == school_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Document request not found")
    item, student = row
    if current_user.role not in STAFF_ROLES and item.requested_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized for this document")

    pdf_bytes = await build_transcript_pdf(session, student)
    if not pdf_bytes:
        raise HTTPException(status_code=404, detail="No approved report cards found for this student")

    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=transcript_{student.student_id}.pdf"},
    )
