"""Generic document repository — staff contracts, ID documents, student
medical/academic records. See services/document_service.py.

Access rules:
- SUPER_ADMIN / SCHOOL_ADMIN / HR: full access to any document in the school.
- A staff member: read-only access to their own (owner_type=staff, owner_id=self).
- A parent: read-only access to their own children's (owner_type=student, owner_id in their children).
"""
from fastapi import APIRouter, Depends, HTTPException, File, Form, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from typing import Optional

from models.document import Document, DocumentOwnerType, DocumentCategory
from models.staff import Staff
from models.user import User, UserRole
from models.audit import SystemAuditLog
from database import get_session
from auth import get_current_user, require_roles
from services.document_service import DocumentService, DocumentServiceError
from routers.parent import get_parent_children_ids

router = APIRouter(prefix="/documents", tags=["Documents"])

DOCUMENT_ADMIN_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR)


def _serialize(doc) -> dict:
    return {
        "id": doc.id,
        "owner_type": doc.owner_type,
        "owner_id": doc.owner_id,
        "category": doc.category,
        "title": doc.title,
        "original_filename": doc.original_filename,
        "content_type": doc.content_type,
        "file_size": doc.file_size,
        "uploaded_by": doc.uploaded_by,
        "access_roles": doc.access_roles,
        "verification_status": getattr(doc, "verification_status", "pending"),
        "verification_notes": getattr(doc, "verification_notes", None),
        "verified_by": getattr(doc, "verified_by", None),
        "verified_at": getattr(doc, "verified_at", None),
        "created_at": doc.created_at,
    }


async def _check_read_access(
    session: AsyncSession, current_user: User, owner_type: DocumentOwnerType, owner_id: str, access_roles: Optional[str] = None,
) -> None:
    if current_user.role in DOCUMENT_ADMIN_ROLES:
        return
    if access_roles and current_user.role.value in [role.strip() for role in access_roles.split(",")]:
        return
    if owner_type == DocumentOwnerType.STAFF and current_user.role == UserRole.TEACHER:
        result = await session.execute(select(Staff).where(Staff.user_id == current_user.id))
        staff = result.scalar_one_or_none()
        if staff and staff.id == owner_id:
            return
    if owner_type == DocumentOwnerType.STUDENT and current_user.role == UserRole.PARENT:
        children_ids = await get_parent_children_ids(current_user, session)
        if owner_id in children_ids:
            return
    raise HTTPException(status_code=403, detail="You do not have access to this document")


@router.post("", response_model=dict)
async def upload_document(
    owner_type: DocumentOwnerType = Form(...),
    owner_id: str = Form(...),
    category: DocumentCategory = Form(...),
    title: Optional[str] = Form(None),
    access_roles: Optional[str] = Form(None),
    file: UploadFile = File(...),
    current_user: User = Depends(require_roles(*DOCUMENT_ADMIN_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    if not current_user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    service = DocumentService(session)
    try:
        doc = await service.upload(current_user.school_id, owner_type, owner_id, category, title, file, current_user.id)
        if access_roles:
            doc.access_roles = access_roles
            session.add(doc)
            await session.commit()
        session.add(SystemAuditLog(actor_id=current_user.id, actor_role=current_user.role.value, action="document.uploaded", entity_type="document", entity_id=doc.id, summary=f"Uploaded {doc.original_filename}", school_id=current_user.school_id))
        await session.commit()
    except DocumentServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize(doc)


@router.get("", response_model=dict)
async def list_documents(
    owner_type: Optional[DocumentOwnerType] = None,
    owner_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if not current_user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    service = DocumentService(session)
    if owner_type and owner_id:
        await _check_read_access(session, current_user, owner_type, owner_id)
        docs = await service.list_for_owner(current_user.school_id, owner_type, owner_id)
    else:
        if current_user.role not in DOCUMENT_ADMIN_ROLES:
            raise HTTPException(status_code=403, detail="Owner is required for this account")
        docs = (await session.execute(select(Document).where(Document.school_id == current_user.school_id).order_by(Document.created_at.desc()))).scalars().all()
    return {"documents": [_serialize(d) for d in docs]}


@router.get("/{document_id}/download")
async def download_document(
    document_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if not current_user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    service = DocumentService(session)
    doc = await service.get(current_user.school_id, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    await _check_read_access(session, current_user, doc.owner_type, doc.owner_id, doc.access_roles)
    session.add(SystemAuditLog(actor_id=current_user.id, actor_role=current_user.role.value, action="document.downloaded", entity_type="document", entity_id=doc.id, summary=f"Downloaded {doc.original_filename}", school_id=current_user.school_id))
    await session.commit()
    return FileResponse(doc.file_path, filename=doc.original_filename, media_type=doc.content_type)


@router.delete("/{document_id}", response_model=dict)
async def delete_document(
    document_id: str,
    current_user: User = Depends(require_roles(*DOCUMENT_ADMIN_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    if not current_user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    service = DocumentService(session)
    try:
        await service.delete(current_user.school_id, document_id)
        session.add(SystemAuditLog(actor_id=current_user.id, actor_role=current_user.role.value, action="document.deleted", entity_type="document", entity_id=document_id, summary="Deleted document", school_id=current_user.school_id))
        await session.commit()
    except DocumentServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "message": "Document deleted"}
