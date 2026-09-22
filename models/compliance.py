"""School-level compliance / accreditation register — distinct from
models.hr_development.StaffCertification (an individual staff member's own
license expiry) and models.facilities.SafetyInspection (a single
facility's safety check): this tracks the school's own standing
obligations to outside bodies (accreditation renewals, regulatory filings,
data-protection registrations, ...) with a due date and status, so nothing
lapses silently.
"""
from datetime import datetime
from enum import Enum
from typing import Optional
import uuid

from sqlmodel import SQLModel, Field


class ComplianceCategory(str, Enum):
    ACCREDITATION = "accreditation"
    REGULATORY = "regulatory"
    SAFETY = "safety"
    DATA_PROTECTION = "data_protection"
    FINANCIAL = "financial"
    OTHER = "other"


class ComplianceStatus(str, Enum):
    UPCOMING = "upcoming"
    COMPLIANT = "compliant"
    OVERDUE = "overdue"
    EXPIRED = "expired"


class ComplianceItem(SQLModel, table=True):
    __tablename__ = "compliance_items"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    name: str
    category: str = ComplianceCategory.OTHER.value
    issuing_body: Optional[str] = None
    requirement_description: Optional[str] = None
    due_date: Optional[str] = Field(default=None, index=True)  # "YYYY-MM-DD"
    status: str = ComplianceStatus.UPCOMING.value
    renewal_frequency_months: Optional[int] = None
    last_reviewed_at: Optional[datetime] = None
    reviewed_by: Optional[str] = None
    notes: Optional[str] = None
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ComplianceItemCreate(SQLModel):
    name: str
    category: ComplianceCategory = ComplianceCategory.OTHER
    issuing_body: Optional[str] = None
    requirement_description: Optional[str] = None
    due_date: Optional[str] = None
    renewal_frequency_months: Optional[int] = None
    notes: Optional[str] = None


class ComplianceItemUpdate(SQLModel):
    name: Optional[str] = None
    category: Optional[ComplianceCategory] = None
    issuing_body: Optional[str] = None
    requirement_description: Optional[str] = None
    due_date: Optional[str] = None
    status: Optional[ComplianceStatus] = None
    renewal_frequency_months: Optional[int] = None
    notes: Optional[str] = None


class ComplianceReviewRequest(SQLModel):
    status: ComplianceStatus
    notes: Optional[str] = None
