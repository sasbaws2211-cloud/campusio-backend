"""Group (many-participant) messaging — additive to the existing 1:1
Message model in models/communication.py, which stays exactly as-is for
sender_id/receiver_id direct messages. A group message is a Message row
with conversation_id set and receiver_id left None (see models/communication.py's
Message.conversation_id field); ConversationParticipant tracks membership
and per-participant read state, since "is_read" on a single Message doesn't
make sense once more than two people can see it.
"""
from datetime import datetime
from typing import Optional
import uuid

from sqlmodel import Field, SQLModel
from sqlalchemy import Column, String, ForeignKey


class Conversation(SQLModel, table=True):
    __tablename__ = "conversations"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    name: str
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ConversationCreate(SQLModel):
    name: str
    participant_ids: list[str]  # User ids; the creator is added automatically


class ConversationParticipant(SQLModel, table=True):
    __tablename__ = "conversation_participants"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    conversation_id: str = Field(sa_column=Column(String, ForeignKey("conversations.id", ondelete="CASCADE"), index=True))
    user_id: str = Field(index=True)
    last_read_at: Optional[datetime] = None
    joined_at: datetime = Field(default_factory=datetime.utcnow)


class GroupMessageCreate(SQLModel):
    content: str
