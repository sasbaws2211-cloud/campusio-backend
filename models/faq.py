"""FAQ / knowledge-base articles — school-scoped, category-tagged Q&A
entries any authenticated user can browse; only admin/HR can author."""
from datetime import datetime
from typing import Optional
import uuid

from sqlmodel import Field, SQLModel


class FaqArticle(SQLModel, table=True):
    __tablename__ = "faq_articles"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    category: str = Field(index=True)
    question: str
    answer: str
    is_published: bool = Field(default=True, index=True)
    view_count: int = 0
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class FaqArticleCreate(SQLModel):
    category: str
    question: str
    answer: str
    is_published: bool = True


class FaqArticleUpdate(SQLModel):
    category: Optional[str] = None
    question: Optional[str] = None
    answer: Optional[str] = None
    is_published: Optional[bool] = None
