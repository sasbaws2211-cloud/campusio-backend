"""Per-user saved widget layout for the Executive Dashboard (see
routers/executive_reports.py's dashboard-layout/available-widgets
endpoints). One row per user — the school-wide default (the original
fixed 6-tile set) is a code constant, not a DB row, so a school with no
customization at all needs no seed data.
"""
from datetime import datetime
from typing import List, Optional
import uuid

from sqlmodel import SQLModel, Field


class DashboardLayout(SQLModel, table=True):
    __tablename__ = "dashboard_layouts"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    user_id: str = Field(index=True, unique=True)  # one saved layout per user
    widgets: str  # JSON-encoded ordered list of widget keys, e.g. '["active_students","fee_overdue_balance","staff_attrition_rate"]'
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class DashboardLayoutUpdate(SQLModel):
    widgets: List[str]
