"""Generic 'parent must confirm they've seen this' tracking — announcements,
report cards, policy documents, or anything else, without changing the
publish flow of any of those existing systems. Opt-in: a staff member
explicitly requests acknowledgement for a specific subject (an
announcement id, a report card id, ...) from a specific set of parents,
rather than every announcement silently requiring one."""
from datetime import datetime
from enum import Enum
from typing import List, Optional
import uuid

from sqlmodel import SQLModel, Field


class AcknowledgementSubjectType(str, Enum):
    ANNOUNCEMENT = "announcement"
    REPORT_CARD = "report_card"
    POLICY_DOCUMENT = "policy_document"
    OTHER = "other"


class Acknowledgement(SQLModel, table=True):
    """subject_type + subject_id is a polymorphic reference rather than a
    real FK — deliberately generic since this must point at rows in
    several unrelated tables (announcements, report_cards, ...)."""
    __tablename__ = "acknowledgements"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    subject_type: str
    subject_id: str = Field(index=True)
    title: str  # human-readable label shown to the parent, snapshotted at request time
    target_user_id: str = Field(index=True)  # the parent (or staff) who must acknowledge
    student_id: Optional[str] = Field(default=None, index=True)  # which child this concerns, if relevant
    acknowledged: bool = False
    acknowledged_at: Optional[datetime] = None
    required_by: Optional[str] = None  # "YYYY-MM-DD" deadline, optional
    requested_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AcknowledgementRequestCreate(SQLModel):
    subject_type: AcknowledgementSubjectType
    subject_id: str
    title: str
    target_user_ids: List[str]
    student_id: Optional[str] = None
    required_by: Optional[str] = None
