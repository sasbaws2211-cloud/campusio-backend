"""Generic document repository service — see models/document.py.

Files are saved to local disk under uploads/documents/<school_id>/
<owner_type>/<owner_id>/<uuid>_<filename>, mirroring the storage convention
in routers/public_admissions.py. Unlike admissions documents, these are
served through an authenticated download endpoint rather than the public
/uploads static mount (routers/documents.py), since staff contracts and
student medical records aren't things a guessable URL should expose.
"""
import logging
import uuid
from pathlib import Path
from typing import Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from fastapi import UploadFile

from models.document import Document, DocumentOwnerType, DocumentCategory
from models.student import Student
from models.staff import Staff

logger = logging.getLogger(__name__)

UPLOAD_DIR = Path("uploads/documents")
ALLOWED_CONTENT_TYPES = {"application/pdf", "image/jpeg", "image/png"}
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB — larger than admissions' 5MB since contracts can be multi-page scans


class DocumentServiceError(Exception):
    pass


class DocumentService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _verify_owner(self, school_id: str, owner_type: DocumentOwnerType, owner_id: str) -> None:
        if owner_type == DocumentOwnerType.STUDENT:
            result = await self.session.execute(
                select(Student).where(Student.id == owner_id, Student.school_id == school_id)
            )
        else:
            result = await self.session.execute(
                select(Staff).where(Staff.id == owner_id, Staff.school_id == school_id)
            )
        if not result.scalar_one_or_none():
            raise DocumentServiceError(f"{owner_type.value.title()} not found")

    async def upload(
        self, school_id: str, owner_type: DocumentOwnerType, owner_id: str,
        category: DocumentCategory, title: Optional[str], file: UploadFile, uploaded_by: str,
    ) -> Document:
        await self._verify_owner(school_id, owner_type, owner_id)

        if file.content_type not in ALLOWED_CONTENT_TYPES:
            raise DocumentServiceError(f"Unsupported file type: {file.content_type}. Allowed: PDF, JPEG, PNG.")
        content = await file.read()
        if len(content) > MAX_FILE_SIZE_BYTES:
            raise DocumentServiceError("File exceeds the 10 MB limit.")
        if len(content) == 0:
            raise DocumentServiceError("File is empty.")

        filename = f"{uuid.uuid4()}_{Path(file.filename or 'document').name}"
        target_dir = UPLOAD_DIR / school_id / owner_type.value / owner_id
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / filename).write_bytes(content)

        doc = Document(
            school_id=school_id, owner_type=owner_type, owner_id=owner_id, category=category,
            title=title, file_path=str(target_dir / filename), original_filename=file.filename or "document",
            content_type=file.content_type, file_size=len(content), uploaded_by=uploaded_by,
        )
        self.session.add(doc)
        await self.session.commit()
        await self.session.refresh(doc)
        return doc

    async def list_for_owner(self, school_id: str, owner_type: DocumentOwnerType, owner_id: str) -> List[Document]:
        result = await self.session.execute(
            select(Document)
            .where(Document.school_id == school_id, Document.owner_type == owner_type, Document.owner_id == owner_id)
            .order_by(Document.created_at.desc())
        )
        return result.scalars().all()

    async def get(self, school_id: str, document_id: str) -> Optional[Document]:
        result = await self.session.execute(
            select(Document).where(Document.id == document_id, Document.school_id == school_id)
        )
        return result.scalar_one_or_none()

    async def delete(self, school_id: str, document_id: str) -> None:
        doc = await self.get(school_id, document_id)
        if not doc:
            raise DocumentServiceError("Document not found")
        path = Path(doc.file_path)
        if path.exists():
            path.unlink()
        await self.session.delete(doc)
        await self.session.commit()
