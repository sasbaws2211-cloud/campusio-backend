"""Structured curriculum planning, lesson delivery, and coverage tracking."""
from datetime import datetime
from typing import Optional
import uuid

from sqlmodel import Field, SQLModel
from sqlalchemy import Column, String, ForeignKey


class CurriculumTopic(SQLModel, table=True):
    __tablename__ = "curriculum_topics"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    class_id: str = Field(index=True)
    subject_id: str = Field(index=True)
    academic_term_id: str = Field(index=True)
    title: str
    sequence: int = 0
    learning_objectives: Optional[str] = None
    competencies: Optional[str] = None
    planned_week: Optional[int] = None
    status: str = "planned"
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class CurriculumTopicCreate(SQLModel):
    class_id: str
    subject_id: str
    academic_term_id: str
    title: str
    sequence: int = 0
    learning_objectives: Optional[str] = None
    competencies: Optional[str] = None
    planned_week: Optional[int] = None


class CurriculumTopicUpdate(SQLModel):
    title: Optional[str] = None
    sequence: Optional[int] = None
    learning_objectives: Optional[str] = None
    competencies: Optional[str] = None
    planned_week: Optional[int] = None
    status: Optional[str] = None


class LessonPlan(SQLModel, table=True):
    __tablename__ = "lesson_plans"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    topic_id: Optional[str] = Field(default=None, sa_column=Column(String, ForeignKey("curriculum_topics.id", ondelete="SET NULL"), index=True))
    teacher_id: str = Field(index=True)
    class_id: str = Field(index=True)
    subject_id: str = Field(index=True)
    lesson_date: str
    week_number: Optional[int] = None
    objectives: Optional[str] = None
    activities: Optional[str] = None
    resources: Optional[str] = None
    notes: Optional[str] = None
    coverage_status: str = "planned"
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class LessonPlanCreate(SQLModel):
    topic_id: Optional[str] = None
    class_id: str
    subject_id: str
    lesson_date: str
    week_number: Optional[int] = None
    objectives: Optional[str] = None
    activities: Optional[str] = None
    resources: Optional[str] = None
    notes: Optional[str] = None
    # Admin/super-admin only — attributes the plan to a specific teacher when
    # the caller has no linked Staff profile of their own. Ignored for a
    # caller who IS a teacher; they can only ever author as themselves.
    teacher_id: Optional[str] = None


class LessonPlanUpdate(SQLModel):
    objectives: Optional[str] = None
    activities: Optional[str] = None
    resources: Optional[str] = None
    notes: Optional[str] = None
    coverage_status: Optional[str] = None


class TopicCoverageUpdate(SQLModel):
    status: str


class TeacherLessonNote(SQLModel, table=True):
    __tablename__ = "teacher_lesson_notes"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    teacher_id: str = Field(index=True)
    class_id: str = Field(index=True)
    subject_id: str = Field(index=True)
    academic_term_id: Optional[str] = Field(default=None, index=True)
    lesson_date: str = Field(index=True)
    content: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class TeacherLessonNoteCreate(SQLModel):
    class_id: str
    subject_id: str
    academic_term_id: Optional[str] = None
    lesson_date: str
    content: str
    # Admin/super-admin only — see LessonPlanCreate.teacher_id.
    teacher_id: Optional[str] = None


class TeacherLessonNoteUpdate(SQLModel):
    content: str


class CurriculumStandard(SQLModel, table=True):
    __tablename__ = "curriculum_standards"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    code: str = Field(index=True)  # e.g. "CCSS.MATH.3.OA.1" or a GES strand code
    title: str
    subject_id: Optional[str] = Field(default=None, index=True)
    strand: Optional[str] = None
    description: Optional[str] = None
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class CurriculumStandardCreate(SQLModel):
    code: str
    title: str
    subject_id: Optional[str] = None
    strand: Optional[str] = None
    description: Optional[str] = None
    is_active: bool = True


class CurriculumStandardUpdate(SQLModel):
    code: Optional[str] = None
    title: Optional[str] = None
    subject_id: Optional[str] = None
    strand: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class TopicStandardLink(SQLModel, table=True):
    __tablename__ = "curriculum_topic_standards"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    topic_id: str = Field(sa_column=Column(String, ForeignKey("curriculum_topics.id", ondelete="CASCADE"), index=True))
    standard_id: str = Field(sa_column=Column(String, ForeignKey("curriculum_standards.id", ondelete="CASCADE"), index=True))


class TopicStandardLinkCreate(SQLModel):
    standard_ids: list[str]