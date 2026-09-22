"""Department registry — Staff.department stays free text (unchanged, used
everywhere already) so this is additive: a school can optionally register
a Department to name a head and get it recognized on the org chart
(routers/hr_admin.py's org-chart endpoint), without migrating every
existing Staff.department value into a hard foreign key."""
from datetime import datetime
from typing import Optional
import uuid

from sqlmodel import Field, SQLModel


class Department(SQLModel, table=True):
    __tablename__ = "hr_departments"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    name: str  # matched against Staff.department by exact string
    head_staff_id: Optional[str] = Field(default=None, index=True)
    description: Optional[str] = None
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class DepartmentCreate(SQLModel):
    name: str
    head_staff_id: Optional[str] = None
    description: Optional[str] = None


class DepartmentUpdate(SQLModel):
    name: Optional[str] = None
    head_staff_id: Optional[str] = None
    description: Optional[str] = None
