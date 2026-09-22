"""Class and Subject models"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey, UniqueConstraint
from typing import Optional
from datetime import datetime
from enum import Enum
import uuid


class ClassLevel(str, Enum):
    KG1 = "kg1"
    KG2 = "kg2"
    PRIMARY_1 = "primary_1"
    PRIMARY_2 = "primary_2"
    PRIMARY_3 = "primary_3"
    PRIMARY_4 = "primary_4"
    PRIMARY_5 = "primary_5"
    PRIMARY_6 = "primary_6"
    JHS_1 = "jhs_1"
    JHS_2 = "jhs_2"
    JHS_3 = "jhs_3"


class Class(SQLModel, table=True):
    __tablename__ = "classes"
    __table_args__ = (UniqueConstraint("school_id", "name", "level", "section", name="uq_classes_school_name_level_section"),)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    name: str
    level: ClassLevel
    section: Optional[str] = None
    capacity: int = 40
    room_number: Optional[str] = None
    academic_term_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("academic_terms.id", ondelete="SET NULL"), index=True)
    )
    campus_id: Optional[str] = Field(default=None, index=True)
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ClassCreate(SQLModel):
    name: str
    level: ClassLevel
    section: Optional[str] = None
    capacity: int = 40
    room_number: Optional[str] = None
    academic_term_id: Optional[str] = None
    campus_id: Optional[str] = None


class ClassUpdate(SQLModel):
    """Partial update — only fields the caller actually sets are applied."""
    name: Optional[str] = None
    level: Optional[ClassLevel] = None
    section: Optional[str] = None
    capacity: Optional[int] = None
    room_number: Optional[str] = None
    academic_term_id: Optional[str] = None
    campus_id: Optional[str] = None
    is_active: Optional[bool] = None


class SubjectCategory(str, Enum):
    CORE = "core"
    ELECTIVE = "elective"


class Subject(SQLModel, table=True):
    __tablename__ = "subjects"
    __table_args__ = (UniqueConstraint("school_id", "code", name="uq_subjects_school_code"),)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    name: str
    code: str
    category: SubjectCategory = SubjectCategory.CORE
    description: Optional[str] = None
    credit_hours: int = 1
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SubjectCreate(SQLModel):
    name: str
    code: str
    category: SubjectCategory = SubjectCategory.CORE
    description: Optional[str] = None
    credit_hours: int = 1


class ClassSubject(SQLModel, table=True):
    __tablename__ = "class_subjects"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    class_id: str = Field(index=True)
    subject_id: str = Field(index=True)
    academic_term_id: str = Field(sa_column=Column(String, ForeignKey("academic_terms.id", ondelete="CASCADE"), index=True))


class ClassWaitlistEntry(SQLModel, table=True):
    __tablename__ = "class_waitlist_entries"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)
    class_id: str = Field(index=True)
    position: int
    requested_at: datetime = Field(default_factory=datetime.utcnow)
    status: str = "waiting"  # waiting | offered | enrolled | cancelled
