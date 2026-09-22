"""Enterprise risk register — operational/financial/reputational/safety
risks scored by likelihood x impact, distinct from
models.compliance.ComplianceItem (regulatory deadlines, not broader
business risk) and models.analytics.RiskLevel (a single student's
academic/attendance risk, not a school-level risk)."""
from datetime import datetime
from enum import Enum
from typing import Optional
import uuid

from sqlmodel import SQLModel, Field


class RiskCategory(str, Enum):
    OPERATIONAL = "operational"
    FINANCIAL = "financial"
    REPUTATIONAL = "reputational"
    SAFETY = "safety"
    STRATEGIC = "strategic"
    COMPLIANCE = "compliance"
    OTHER = "other"


class RiskStatus(str, Enum):
    OPEN = "open"
    MITIGATING = "mitigating"
    CLOSED = "closed"


class RiskRegisterItem(SQLModel, table=True):
    __tablename__ = "risk_register_items"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    title: str
    category: str = RiskCategory.OTHER.value
    description: Optional[str] = None
    likelihood: int = Field(ge=1, le=5, default=3)  # 1=rare, 5=almost certain
    impact: int = Field(ge=1, le=5, default=3)  # 1=negligible, 5=severe
    mitigation_plan: Optional[str] = None
    owner_id: Optional[str] = None
    status: str = RiskStatus.OPEN.value
    review_date: Optional[str] = None
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class RiskRegisterItemCreate(SQLModel):
    title: str
    category: RiskCategory = RiskCategory.OTHER
    description: Optional[str] = None
    likelihood: int = Field(ge=1, le=5, default=3)
    impact: int = Field(ge=1, le=5, default=3)
    mitigation_plan: Optional[str] = None
    owner_id: Optional[str] = None
    review_date: Optional[str] = None


class RiskRegisterItemUpdate(SQLModel):
    title: Optional[str] = None
    category: Optional[RiskCategory] = None
    description: Optional[str] = None
    likelihood: Optional[int] = Field(default=None, ge=1, le=5)
    impact: Optional[int] = Field(default=None, ge=1, le=5)
    mitigation_plan: Optional[str] = None
    owner_id: Optional[str] = None
    status: Optional[RiskStatus] = None
    review_date: Optional[str] = None
