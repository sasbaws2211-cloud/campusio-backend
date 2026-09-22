"""Configurable surveys, responses, and feedback evaluations."""
from datetime import datetime
from typing import Optional
import uuid

from sqlmodel import Field, SQLModel


class Survey(SQLModel, table=True):
    __tablename__ = "surveys"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    title: str
    description: Optional[str] = None
    survey_type: str = "satisfaction"
    questions_json: str = "[]"
    audience: str = "all"
    anonymous: bool = False
    status: str = "draft"
    opens_at: Optional[str] = None
    closes_at: Optional[str] = None
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class SurveyCreate(SQLModel):
    title: str
    description: Optional[str] = None
    survey_type: str = "satisfaction"
    questions_json: str = "[]"
    audience: str = "all"
    anonymous: bool = False
    status: str = "draft"
    opens_at: Optional[str] = None
    closes_at: Optional[str] = None


class SurveyUpdate(SQLModel):
    title: Optional[str] = None
    description: Optional[str] = None
    questions_json: Optional[str] = None
    audience: Optional[str] = None
    anonymous: Optional[bool] = None
    status: Optional[str] = None
    opens_at: Optional[str] = None
    closes_at: Optional[str] = None


class SurveyResponse(SQLModel, table=True):
    __tablename__ = "survey_responses"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    survey_id: str = Field(index=True)
    respondent_id: Optional[str] = Field(default=None, index=True)
    answers_json: str = "{}"
    rating: Optional[float] = None
    feedback: Optional[str] = None
    submitted_at: datetime = Field(default_factory=datetime.utcnow)


class SurveyResponseCreate(SQLModel):
    answers_json: str = "{}"
    rating: Optional[float] = None
    feedback: Optional[str] = None