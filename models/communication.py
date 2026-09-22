"""Communication models - Announcements and Messages"""
from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime
from enum import Enum
import uuid


class AnnouncementType(str, Enum):
    GENERAL = "general"
    ACADEMIC = "academic"
    EVENT = "event"
    EMERGENCY = "emergency"
    FEE = "fee"


class AnnouncementAudience(str, Enum):
    ALL = "all"
    TEACHERS = "teachers"
    STUDENTS = "students"
    PARENTS = "parents"
    SPECIFIC_CLASS = "specific_class"


class MessageType(str, Enum):
    """Message context type for filtering and categorization"""
    GENERAL = "general"
    GRADE = "grade"
    ATTENDANCE = "attendance"
    BEHAVIOR = "behavior"
    FEE = "fee"
    URGENT = "urgent"


class Announcement(SQLModel, table=True):
    __tablename__ = "announcements"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    title: str
    content: str
    announcement_type: AnnouncementType
    audience: AnnouncementAudience
    target_class_id: Optional[str] = Field(default=None, index=True)
    is_published: bool = True
    publish_date: str
    expiry_date: Optional[str] = None
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AnnouncementCreate(SQLModel):
    title: str
    content: str
    announcement_type: AnnouncementType
    audience: AnnouncementAudience
    target_class_id: Optional[str] = None
    is_published: bool = True
    publish_date: str
    expiry_date: Optional[str] = None


class Message(SQLModel, table=True):
    __tablename__ = "messages"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    sender_id: str = Field(index=True)
    receiver_id: Optional[str] = Field(default=None, index=True)  # None for a group message (see conversation_id)
    subject: str
    content: str
    is_read: bool = False
    read_at: Optional[datetime] = None  # When message was read
    parent_message_id: Optional[str] = Field(default=None, index=True)  # For threading
    student_id: Optional[str] = Field(default=None, index=True)  # Which student conversation is about
    class_id: Optional[str] = Field(default=None, index=True)  # Which class context
    message_type: MessageType = MessageType.GENERAL  # Type of message for filtering
    conversation_id: Optional[str] = Field(default=None, index=True)  # Set for group-conversation messages; see models/group_conversation.py
    created_at: datetime = Field(default_factory=datetime.utcnow)


class MessageCreate(SQLModel):
    receiver_id: str
    subject: str
    content: str
    parent_message_id: Optional[str] = None
    student_id: Optional[str] = None
    class_id: Optional[str] = None
    message_type: MessageType = MessageType.GENERAL


class EmailNotification(SQLModel, table=True):
    __tablename__ = "email_notifications"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    recipient_email: str
    subject: str
    content: str
    notification_type: str
    status: str = "pending"
    error_message: Optional[str] = None
    sent_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SMSNotification(SQLModel, table=True):
    """Track SMS notifications sent"""
    __tablename__ = "sms_notifications"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    recipient_phone: str = Field(index=True)
    recipient_name: Optional[str] = None
    message: str
    notification_type: str  # "fee_reminder", "attendance", "announcement", "grade", "general"
    status: str = "pending"  # pending, sent, failed, delivered
    message_id: Optional[str] = None  # ID from USMS API
    error_message: Optional[str] = None
    sent_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class SendSMSRequest(SQLModel):
    """Request schema for sending SMS"""
    phone_numbers: list[str]
    message: str
    message_type: str = "text"


class SendFeeSMSRequest(SQLModel):
    """Request schema for fee reminder SMS"""
    student_id: str
    amount_due: float
    due_date: str


class SendAttendanceSMSRequest(SQLModel):
    """Request schema for attendance notification SMS"""
    student_id: str
    phone_number: str
    attendance_percentage: float


class SendAnnouncementSMSRequest(SQLModel):
    """Request schema for announcement SMS"""
    announcement_id: str
    phone_numbers: list[str]
    title: str
    snippet: str
