"""General-purpose consent forms / digital permission slips (field trips,
media releases, data sharing) — deliberately separate from
models.student_support_enterprise.ParentConsent, which is safeguarding/SEN
-specific: gated to safeguarding-cleared staff, staff-entered on the
parent's behalf, under /student-support/secure. This module is the
opposite shape on purpose: any staff member can publish a form, and the
parent responds themselves — a field-trip permission slip has nothing to
do with child-protection access control.
"""
from datetime import date, datetime
from enum import Enum
from typing import List, Optional
import uuid

from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey


class ConsentResponseStatus(str, Enum):
    PENDING = "pending"
    GRANTED = "granted"
    DECLINED = "declined"


class ConsentForm(SQLModel, table=True):
    """One published form — e.g. "Museum trip, Oct 12" or "Photo/media
    release, 2026/2027". consent_type is free text (field_trip,
    media_release, data_sharing, other) so a school can add its own
    without a migration."""
    __tablename__ = "consent_forms"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    title: str
    description: str
    consent_type: str = "other"
    target_class_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("classes.id", ondelete="SET NULL"), index=True),
    )
    respond_by: Optional[str] = None  # "YYYY-MM-DD" deadline, optional
    is_active: bool = True
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ConsentFormCreate(SQLModel):
    title: str
    description: str
    consent_type: str = "other"
    target_class_id: Optional[str] = None
    target_student_ids: Optional[List[str]] = None  # ad-hoc targeting when not a whole class
    respond_by: Optional[str] = None


class ConsentResponse(SQLModel, table=True):
    """One row per (form, student) — the specific parent's decision for
    their child. A form targeting a whole class creates one of these per
    enrolled student at publish time."""
    __tablename__ = "consent_responses"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    consent_form_id: str = Field(sa_column=Column(String, ForeignKey("consent_forms.id", ondelete="CASCADE"), index=True))
    student_id: str = Field(sa_column=Column(String, ForeignKey("students.id", ondelete="CASCADE"), index=True))
    parent_user_id: Optional[str] = Field(default=None, index=True)
    status: str = ConsentResponseStatus.PENDING.value
    notes: Optional[str] = None
    responded_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ConsentResponseSubmit(SQLModel):
    status: ConsentResponseStatus
    notes: Optional[str] = None
