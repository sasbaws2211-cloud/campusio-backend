"""Succession planning — which key positions have an identified successor
and how ready they are, independent of any live vacancy (this is forward
planning, not recruitment; see models/hr_recruitment.py for actual hiring)."""
from datetime import datetime
from typing import Optional
import uuid

from sqlmodel import Field, SQLModel


class SuccessionPlan(SQLModel, table=True):
    __tablename__ = "hr_succession_plans"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    position_title: str
    department: Optional[str] = None
    current_holder_staff_id: Optional[str] = Field(default=None, index=True)  # None if currently vacant
    successor_staff_id: Optional[str] = Field(default=None, index=True)  # None if no successor identified yet
    readiness: str = "not_ready"  # ready_now/1_2_years/3_5_years/not_ready
    development_notes: Optional[str] = None
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class SuccessionPlanCreate(SQLModel):
    position_title: str
    department: Optional[str] = None
    current_holder_staff_id: Optional[str] = None
    successor_staff_id: Optional[str] = None
    readiness: str = "not_ready"
    development_notes: Optional[str] = None


class SuccessionPlanUpdate(SQLModel):
    successor_staff_id: Optional[str] = None
    readiness: Optional[str] = None
    development_notes: Optional[str] = None
