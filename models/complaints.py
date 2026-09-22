"""Parent/student complaint and grievance tracking — deliberately separate
from models.ticket.Ticket, which is restricted to admin roles as a
platform-support channel (Campusio staff <-> school admin), not a
parent-to-school complaint channel. Same shape (thread of comments,
severity, resolution), different audience and access control."""
from datetime import datetime
from enum import Enum
from typing import Optional
import uuid

from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey


class ComplaintCategory(str, Enum):
    ACADEMIC = "academic"
    BULLYING = "bullying"
    FACILITY = "facility"
    STAFF_CONDUCT = "staff_conduct"
    BILLING = "billing"
    TRANSPORT = "transport"
    OTHER = "other"


class ComplaintSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ComplaintStatus(str, Enum):
    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class Complaint(SQLModel, table=True):
    __tablename__ = "complaints"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    submitted_by: str = Field(index=True)  # User.id of the parent/student who filed it
    student_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("students.id", ondelete="SET NULL"), index=True),
    )
    category: str = ComplaintCategory.OTHER.value
    severity: str = ComplaintSeverity.MEDIUM.value
    subject: str
    description: str
    status: str = ComplaintStatus.SUBMITTED.value
    assigned_to: Optional[str] = Field(default=None, index=True)
    resolution_notes: Optional[str] = None
    resolved_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ComplaintCreate(SQLModel):
    student_id: Optional[str] = None
    category: ComplaintCategory = ComplaintCategory.OTHER
    severity: ComplaintSeverity = ComplaintSeverity.MEDIUM
    subject: str
    description: str


class ComplaintUpdate(SQLModel):
    status: Optional[ComplaintStatus] = None
    assigned_to: Optional[str] = None
    resolution_notes: Optional[str] = None


class ComplaintComment(SQLModel, table=True):
    __tablename__ = "complaint_comments"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    complaint_id: str = Field(sa_column=Column(String, ForeignKey("complaints.id", ondelete="CASCADE"), index=True))
    author_id: str = Field(index=True)
    comment: str
    # Staff-only notes never shown to the parent who filed the complaint —
    # same is_internal convention as models.ticket.TicketComment.
    is_internal: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ComplaintCommentCreate(SQLModel):
    comment: str
    is_internal: bool = False
