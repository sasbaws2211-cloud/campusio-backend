"""Generic document repository — staff contracts, ID documents, student
medical/academic records, etc. Distinct from the narrow file-attachment
concepts already in this codebase (ApplicantDocument for admissions,
CertificateIssuance for generated certificates, library book copies): this
is a general-purpose "attach a file to a Student or Staff record" store,
saved to local disk under uploads/documents/ (same convention as
public_admissions.py) but served through an authenticated download endpoint
rather than the public /uploads static mount, since these files can be
sensitive (contracts, medical records).
"""
from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime
from enum import Enum
import uuid


class DocumentOwnerType(str, Enum):
    STUDENT = "student"
    STAFF = "staff"


class DocumentCategory(str, Enum):
    CONTRACT = "contract"
    ID_DOCUMENT = "id_document"
    CERTIFICATE = "certificate"
    MEDICAL = "medical"
    ACADEMIC_RECORD = "academic_record"
    OTHER = "other"


class Document(SQLModel, table=True):
    __tablename__ = "documents"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    owner_type: DocumentOwnerType
    owner_id: str = Field(index=True)  # Student.id or Staff.id, depending on owner_type
    category: DocumentCategory
    title: Optional[str] = None
    file_path: str  # disk path, not web-servable directly
    original_filename: str
    content_type: str
    file_size: int
    uploaded_by: str  # User.id
    access_roles: Optional[str] = None  # comma-separated roles; admins always retain access
    created_at: datetime = Field(default_factory=datetime.utcnow)
