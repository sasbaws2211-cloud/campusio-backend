"""Login attempt log — the record account lockout (auth.py:check_login_allowed)
reads to decide whether an email is temporarily locked out, and also this
app's actual "who tried to log in, when, from where, and did it work" audit
trail. Deliberately separate from models/audit.py's SystemAuditLog, whose
actor_id is a required real user id — a failed login against an unknown
email, or the wrong password for a real one, has no authenticated actor to
attribute it to.
"""
from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime
import uuid


class LoginAttempt(SQLModel, table=True):
    __tablename__ = "login_attempts"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    email_attempted: str = Field(index=True)
    user_id: Optional[str] = Field(default=None, index=True)  # set only when the email matched a real user
    success: bool = Field(index=True)
    failure_reason: Optional[str] = None  # "invalid_credentials" | "locked_out" | "account_disabled" | "invalid_otp"
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
