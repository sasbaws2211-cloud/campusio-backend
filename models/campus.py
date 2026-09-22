"""Campus Management Models (light multi-campus support)

A Campus is a label/filter for reporting, not a tenancy boundary — every
table in this app is still scoped by school_id, and campus_id is an
optional, nullable field added to Student/Staff/Class only (the three
entities campus-based reporting actually needs). No other table or
existing query logic changes."""
from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime
import uuid


class Campus(SQLModel, table=True):
    __tablename__ = "campuses"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)

    name: str
    address: Optional[str] = None
    phone: Optional[str] = None
    is_main_campus: bool = False

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class CampusCreate(SQLModel):
    name: str
    address: Optional[str] = None
    phone: Optional[str] = None
    is_main_campus: bool = False


class CampusUpdate(SQLModel):
    name: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    is_main_campus: Optional[bool] = None
