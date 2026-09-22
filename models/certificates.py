"""Certificate / ID Card Generation Models"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey
from typing import Optional
from datetime import datetime
from enum import Enum
import uuid


class CertificateType(str, Enum):
    LEAVING = "leaving"
    TRANSFER = "transfer"
    ACHIEVEMENT = "achievement"


class PersonType(str, Enum):
    STUDENT = "student"
    STAFF = "staff"
    PICKUP_PERSON = "pickup_person"


class IDCardStatus(str, Enum):
    ACTIVE = "active"
    LOST = "lost"
    REVOKED = "revoked"
    REISSUED = "reissued"


class CertificateTemplate(SQLModel, table=True):
    """A school-customized Jinja2 template for a certificate type. When a
    school has no template row for a given type, generation falls back to
    the fixed file in templates/ (e.g. certificate_leaving.html) — see
    services/certificate_pdf_service.py::render_html."""
    __tablename__ = "certificate_templates"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)

    certificate_type: CertificateType
    name: str
    html_content: str
    is_default: bool = True

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class CertificateTemplateCreate(SQLModel):
    certificate_type: CertificateType
    name: str
    html_content: str
    is_default: bool = True


class CertificateTemplateUpdate(SQLModel):
    name: Optional[str] = None
    html_content: Optional[str] = None
    is_default: Optional[bool] = None


class CertificateIssuance(SQLModel, table=True):
    """An audit/reprint log of certificates generated for a student"""
    __tablename__ = "certificate_issuances"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(sa_column=Column(String, ForeignKey("students.id", ondelete="CASCADE"), index=True))

    certificate_type: CertificateType
    certificate_number: str = Field(unique=True, index=True)
    issue_date: str
    issued_by: Optional[str] = None
    remarks: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)


class CertificateIssuanceCreate(SQLModel):
    remarks: Optional[str] = None


class IDCard(SQLModel, table=True):
    """A durable, long-lived ID card for a student or staff member.

    qr_payload is the card's own id/card_number — static and non-expiring,
    unlike models/security.py's DailyQRToken which is deliberately
    single-day/single-use for a different (parent-pickup) safety workflow.
    A printed card must stay scannable indefinitely.
    """
    __tablename__ = "id_cards"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)

    person_type: PersonType
    person_id: str = Field(index=True)

    card_number: str = Field(unique=True, index=True)
    issue_date: str
    expiry_date: Optional[str] = None
    status: IDCardStatus = IDCardStatus.ACTIVE
    qr_payload: str

    # Distribution — separate from "generated", since a card can sit
    # printed-but-uncollected at the office for a while. A dedicated
    # action (see routers/id_cards.py::distribute_id_card) rather than a
    # generic field on IDCardUpdate, so it stays auditable — same pattern
    # as AcademicTerm's lock/unlock actions.
    distributed_at: Optional[datetime] = None
    distributed_by: Optional[str] = None  # User.id of the staff member who handed it out
    distributed_note: Optional[str] = None  # e.g. "collected by mother"

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class IDCardCreate(SQLModel):
    expiry_date: Optional[str] = None


class IDCardUpdate(SQLModel):
    status: Optional[IDCardStatus] = None
    expiry_date: Optional[str] = None


class IDCardDistribute(SQLModel):
    note: Optional[str] = None


class BulkIDCardRequest(SQLModel):
    person_type: PersonType
    person_ids: list[str]
    expiry_date: Optional[str] = None
