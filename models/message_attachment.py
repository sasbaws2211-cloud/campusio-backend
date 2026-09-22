"""File attachments on a Message — mirrors models/document.py's shape and
services/document_service.py's local-disk storage convention, but scoped to
a message_id rather than a student/staff owner, and served through
communication.py's own message-access checks rather than documents.py's.
"""
from datetime import datetime
import uuid

from sqlmodel import Field, SQLModel
from sqlalchemy import Column, String, ForeignKey


class MessageAttachment(SQLModel, table=True):
    __tablename__ = "message_attachments"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    message_id: str = Field(sa_column=Column(String, ForeignKey("messages.id", ondelete="CASCADE"), index=True))
    file_path: str
    original_filename: str
    content_type: str
    file_size: int
    uploaded_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
