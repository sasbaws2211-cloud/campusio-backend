"""Strategic goal / OKR tracking — lets leadership set a multi-year target
tied to a metric (optionally one already surfaced by
routers/executive_reports.py or routers/strategic_reports.py, e.g.
"retention_rate", but free-text so any metric name works) and log
progress against it over time. Nothing else in this app lets a target be
set and tracked — every other dashboard is a live snapshot with no goal
attached to it.
"""
from datetime import datetime
from enum import Enum
from typing import Optional
import uuid

from sqlmodel import SQLModel, Field


class GoalStatus(str, Enum):
    NOT_STARTED = "not_started"
    ON_TRACK = "on_track"
    AT_RISK = "at_risk"
    ACHIEVED = "achieved"
    MISSED = "missed"


class StrategicGoal(SQLModel, table=True):
    __tablename__ = "strategic_goals"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    title: str
    description: Optional[str] = None
    category: str = "other"  # academic, financial, operational, hr, community, other
    target_metric: Optional[str] = None  # free-text label, e.g. "retention_rate", "fee_collection_rate"
    target_value: Optional[float] = None
    current_value: Optional[float] = None
    unit: Optional[str] = None  # "%", "GHS", "students", ...
    start_date: str
    target_date: str
    status: str = GoalStatus.NOT_STARTED.value
    owner_id: Optional[str] = None  # User.id of whoever is accountable
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class StrategicGoalCreate(SQLModel):
    title: str
    description: Optional[str] = None
    category: str = "other"
    target_metric: Optional[str] = None
    target_value: Optional[float] = None
    current_value: Optional[float] = None
    unit: Optional[str] = None
    start_date: str
    target_date: str
    owner_id: Optional[str] = None


class StrategicGoalUpdate(SQLModel):
    title: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    target_metric: Optional[str] = None
    target_value: Optional[float] = None
    unit: Optional[str] = None
    start_date: Optional[str] = None
    target_date: Optional[str] = None
    status: Optional[GoalStatus] = None
    owner_id: Optional[str] = None


class StrategicGoalProgressUpdate(SQLModel):
    current_value: float
    status: Optional[GoalStatus] = None
