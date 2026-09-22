"""Elective / subject-track management — per-student subject pathways
distinct from ClassSubject (whole-class, models.classroom) and
StudentEnrollment (class-level, models.student)."""
from datetime import datetime
from typing import Optional
import uuid

from sqlmodel import Field, SQLModel
from sqlalchemy import Column, String, ForeignKey


class Track(SQLModel, table=True):
    __tablename__ = "tracks"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    name: str
    academic_term_id: Optional[str] = Field(default=None, index=True)
    # models.classroom.ClassLevel value, or None = open to every level.
    # Plain string column (same convention as models/grade.py::GradingScheme
    # .class_level) — deliberately NOT typed as the ClassLevel Enum, so it
    # never becomes a native Postgres enum requiring its own migration for
    # every future level added.
    class_level: Optional[str] = Field(default=None, index=True)
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class TrackCreate(SQLModel):
    name: str
    academic_term_id: Optional[str] = None
    class_level: Optional[str] = None
    is_active: bool = True


class TrackUpdate(SQLModel):
    name: Optional[str] = None
    academic_term_id: Optional[str] = None
    class_level: Optional[str] = None
    is_active: Optional[bool] = None


class TrackSubject(SQLModel, table=True):
    __tablename__ = "track_subjects"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    track_id: str = Field(sa_column=Column(String, ForeignKey("tracks.id", ondelete="CASCADE"), index=True))
    subject_id: str = Field(index=True)


class TrackSubjectCreate(SQLModel):
    subject_id: str


class StudentTrack(SQLModel, table=True):
    __tablename__ = "student_tracks"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)
    track_id: str = Field(sa_column=Column(String, ForeignKey("tracks.id", ondelete="CASCADE"), index=True))
    academic_term_id: str = Field(index=True)
    enrolled_at: datetime = Field(default_factory=datetime.utcnow)


class StudentTrackCreate(SQLModel):
    student_id: str
    academic_term_id: str
