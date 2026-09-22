"""Web Push subscriptions and per-user notification preferences.

PushSubscription stores the browser-issued subscription object (endpoint +
encryption keys) from the Push API — see services/push_notification_service.py
for how it's used to actually send a push. NotificationPreference is a
per-user settings row (typed booleans, not JSON — same convention as
models/fee_reminders.py's per-school FeeReminderSettings) gating whether a
given channel/event actually fires for that user.
"""
from datetime import datetime
from typing import Optional
import uuid

from sqlmodel import Field, SQLModel


class PushSubscription(SQLModel, table=True):
    __tablename__ = "push_subscriptions"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    user_id: str = Field(index=True)
    endpoint: str = Field(index=True, unique=True)
    p256dh_key: str
    auth_key: str
    user_agent: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class PushSubscriptionCreate(SQLModel):
    endpoint: str
    p256dh_key: str
    auth_key: str
    user_agent: Optional[str] = None


class NotificationPreference(SQLModel, table=True):
    __tablename__ = "notification_preferences"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    user_id: str = Field(index=True, unique=True)
    push_new_message: bool = True
    push_announcements: bool = True
    in_app_new_message: bool = True
    in_app_announcements: bool = True
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class NotificationPreferenceUpdate(SQLModel):
    push_new_message: Optional[bool] = None
    push_announcements: Optional[bool] = None
    in_app_new_message: Optional[bool] = None
    in_app_announcements: Optional[bool] = None
