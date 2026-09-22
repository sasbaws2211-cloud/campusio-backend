"""Post-hire onboarding, training, and staff certification compliance."""
from datetime import datetime
from typing import Optional
import uuid

from sqlmodel import Field, SQLModel
from sqlalchemy import Column, String, ForeignKey


class OnboardingTaskStatus(str):
    PENDING = "pending"
    COMPLETED = "completed"
    WAIVED = "waived"


class StaffOnboardingTask(SQLModel, table=True):
    __tablename__ = "hr_onboarding_tasks"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    staff_id: str = Field(sa_column=Column(String, ForeignKey("staff.id", ondelete="CASCADE"), index=True))
    title: str
    description: Optional[str] = None
    due_date: Optional[str] = None
    status: str = OnboardingTaskStatus.PENDING
    completed_at: Optional[datetime] = None
    completed_by: Optional[str] = None
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class OnboardingTaskCreate(SQLModel):
    staff_id: str
    title: str
    description: Optional[str] = None
    due_date: Optional[str] = None


class OnboardingTaskStatusUpdate(SQLModel):
    status: str


class StaffTraining(SQLModel, table=True):
    __tablename__ = "hr_staff_training"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    staff_id: str = Field(sa_column=Column(String, ForeignKey("staff.id", ondelete="CASCADE"), index=True))
    title: str
    provider: Optional[str] = None
    start_date: Optional[str] = None
    completion_date: Optional[str] = None
    hours: Optional[float] = None
    outcome: Optional[str] = None
    notes: Optional[str] = None
    # ROI tracking — cost is known at booking time; impact is necessarily
    # filled in later, once there's been time to observe whether the
    # training actually changed anything (hence a separate optional field
    # rather than required at creation).
    cost: Optional[float] = None
    performance_impact_rating: Optional[int] = None  # 1-5, set after the fact
    roi_notes: Optional[str] = None
    recorded_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class StaffTrainingCreate(SQLModel):
    staff_id: str
    title: str
    provider: Optional[str] = None
    start_date: Optional[str] = None
    completion_date: Optional[str] = None
    hours: Optional[float] = None
    outcome: Optional[str] = None
    notes: Optional[str] = None
    cost: Optional[float] = None


class StaffTrainingImpactUpdate(SQLModel):
    performance_impact_rating: int  # 1-5
    roi_notes: Optional[str] = None


class StaffCertification(SQLModel, table=True):
    __tablename__ = "hr_staff_certifications"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    staff_id: str = Field(sa_column=Column(String, ForeignKey("staff.id", ondelete="CASCADE"), index=True))
    name: str
    issuing_body: Optional[str] = None
    certificate_number: Optional[str] = None
    issued_date: Optional[str] = None
    expiry_date: Optional[str] = Field(default=None, index=True)
    document_id: Optional[str] = None
    notes: Optional[str] = None
    recorded_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class StaffCertificationCreate(SQLModel):
    staff_id: str
    name: str
    issuing_body: Optional[str] = None
    certificate_number: Optional[str] = None
    issued_date: Optional[str] = None
    expiry_date: Optional[str] = None
    document_id: Optional[str] = None
    notes: Optional[str] = None