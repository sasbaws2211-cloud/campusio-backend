"""E-library models for school digital resources."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from enum import Enum
import uuid

from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, Integer, ForeignKey


class LibraryMaterialType(str, Enum):
    BOOK = "book"
    TEXTBOOK = "textbook"
    ACADEMIC_MATERIAL = "academic_material"
    EDUCATIONAL_VIDEO = "educational_video"
    WORKSHEET = "worksheet"
    REFERENCE = "reference"


class LibraryContentType(str, Enum):
    PDF = "pdf"
    VIDEO = "video"
    DOCUMENT = "document"
    AUDIO = "audio"
    LINK = "link"


class LibraryInteractionType(str, Enum):
    VIEW = "view"
    DOWNLOAD = "download"


class LibraryCategory(SQLModel, table=True):
    __tablename__ = "library_categories"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    name: str
    slug: str = Field(index=True)
    description: Optional[str] = None
    parent_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("library_categories.id", ondelete="SET NULL"), index=True),
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class LibraryCategoryCreate(SQLModel):
    name: str
    slug: Optional[str] = None
    description: Optional[str] = None
    parent_id: Optional[str] = None


class LibraryCategoryUpdate(SQLModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    parent_id: Optional[str] = None


class LibraryTag(SQLModel, table=True):
    __tablename__ = "library_tags"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    name: str
    slug: str = Field(index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class LibraryItem(SQLModel, table=True):
    __tablename__ = "library_items"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    title: str
    description: Optional[str] = None
    material_type: str
    content_type: str
    category_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("library_categories.id", ondelete="SET NULL"), index=True),
    )
    subject_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("subjects.id", ondelete="SET NULL"), index=True),
    )
    author: Optional[str] = None
    publisher: Optional[str] = None
    edition: Optional[str] = None
    language: Optional[str] = None
    isbn: Optional[str] = None
    publication_year: Optional[int] = None
    file_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    external_url: Optional[str] = None
    view_count: int = Field(default=0, sa_column=Column(Integer, nullable=False, server_default="0"))
    download_count: int = Field(default=0, sa_column=Column(Integer, nullable=False, server_default="0"))
    is_published: bool = True
    is_featured: bool = False
    # Licensing — beyond the existing published/class-restricted visibility
    # (LibraryItemClass): staff_only excludes students/parents outright,
    # regardless of class assignment. max_concurrent_readers enforces a
    # licensed digital resource's simultaneous-reader cap via
    # LibraryItemAccessSession below; None means unlimited (today's
    # behavior, unchanged for every item that doesn't set it).
    staff_only: bool = False
    max_concurrent_readers: Optional[int] = None
    created_by: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("users.id", ondelete="SET NULL"), index=True),
    )
    updated_by: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("users.id", ondelete="SET NULL")),
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class LibraryItemUpdate(SQLModel):
    """Documents the partial-update shape; the endpoint itself takes multipart Form fields."""
    title: Optional[str] = None
    description: Optional[str] = None
    material_type: Optional[str] = None
    content_type: Optional[str] = None
    category_id: Optional[str] = None
    subject_id: Optional[str] = None
    author: Optional[str] = None
    publisher: Optional[str] = None
    edition: Optional[str] = None
    language: Optional[str] = None
    isbn: Optional[str] = None
    publication_year: Optional[int] = None
    class_ids: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    external_url: Optional[str] = None
    is_published: Optional[bool] = None
    is_featured: Optional[bool] = None
    staff_only: Optional[bool] = None
    max_concurrent_readers: Optional[int] = None


class LibraryItemTag(SQLModel, table=True):
    __tablename__ = "library_item_tags"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    item_id: str = Field(sa_column=Column(String, ForeignKey("library_items.id", ondelete="CASCADE"), index=True))
    tag_id: str = Field(sa_column=Column(String, ForeignKey("library_tags.id", ondelete="CASCADE"), index=True))


class LibraryItemClass(SQLModel, table=True):
    __tablename__ = "library_item_classes"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    item_id: str = Field(sa_column=Column(String, ForeignKey("library_items.id", ondelete="CASCADE"), index=True))
    class_id: str = Field(sa_column=Column(String, ForeignKey("classes.id", ondelete="CASCADE"), index=True))


class LibraryItemFavorite(SQLModel, table=True):
    __tablename__ = "library_item_favorites"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    item_id: str = Field(sa_column=Column(String, ForeignKey("library_items.id", ondelete="CASCADE"), index=True))
    user_id: str = Field(sa_column=Column(String, ForeignKey("users.id", ondelete="CASCADE"), index=True))
    created_at: datetime = Field(default_factory=datetime.utcnow)


class LibraryItemRating(SQLModel, table=True):
    __tablename__ = "library_item_ratings"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    item_id: str = Field(sa_column=Column(String, ForeignKey("library_items.id", ondelete="CASCADE"), index=True))
    user_id: str = Field(sa_column=Column(String, ForeignKey("users.id", ondelete="CASCADE"), index=True))
    rating: int
    review: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class LibraryItemRatingCreate(SQLModel):
    rating: int
    review: Optional[str] = None


class LibraryItemInteraction(SQLModel, table=True):
    __tablename__ = "library_item_interactions"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    item_id: str = Field(sa_column=Column(String, ForeignKey("library_items.id", ondelete="CASCADE"), index=True))
    user_id: str = Field(sa_column=Column(String, ForeignKey("users.id", ondelete="CASCADE"), index=True))
    interaction_type: str
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class LibraryItemAccessSession(SQLModel, table=True):
    """A claimed 'seat' against LibraryItem.max_concurrent_readers — exists
    only for items with a cap set; unlimited items never create these.
    A session simply expires (checked at query time, no cleanup job needed)
    rather than requiring an explicit close, so a reader who just closes
    the browser tab doesn't permanently hold a seat."""
    __tablename__ = "library_item_access_sessions"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    item_id: str = Field(sa_column=Column(String, ForeignKey("library_items.id", ondelete="CASCADE"), index=True))
    user_id: str = Field(sa_column=Column(String, ForeignKey("users.id", ondelete="CASCADE"), index=True))
    started_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime
