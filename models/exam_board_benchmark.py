"""Admin-entered reference data for comparing this school's external
exam-board results (models/exam_board.py::ExamBoardResult) against
published national/regional averages — WAEC/BECE don't expose an API for
this, so a staff member enters the published average once per
exam/year/subject and routers/executive_reports.py compares this school's
own average against it."""
from datetime import datetime
from typing import Optional
import uuid

from sqlmodel import SQLModel, Field


class ExamBoardBenchmark(SQLModel, table=True):
    __tablename__ = "exam_board_benchmarks"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    exam_name: str = Field(index=True)
    exam_year: str = Field(index=True)
    subject_id: str = Field(index=True)
    national_average_score: Optional[float] = None
    regional_average_score: Optional[float] = None
    source: Optional[str] = None  # e.g. "WAEC 2026 Chief Examiner's Report"
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ExamBoardBenchmarkCreate(SQLModel):
    exam_name: str
    exam_year: str
    subject_id: str
    national_average_score: Optional[float] = None
    regional_average_score: Optional[float] = None
    source: Optional[str] = None


class ExamBoardBenchmarkUpdate(SQLModel):
    national_average_score: Optional[float] = None
    regional_average_score: Optional[float] = None
    source: Optional[str] = None
