"""GL Audit Log models for complete audit trail of all GL changes

Every change to GL accounts, journal entries, and expenses is logged for:
- Regulatory compliance (audit trail requirements)
- Fraud detection (who changed what and when)
- Problem diagnosis (trace changes leading to issues)
- User accountability (track user actions)
"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Enum as SQLEnum, Column
from typing import Optional, Dict, Any
from datetime import datetime
from enum import Enum
import uuid
import json


class AuditActionType(str, Enum):
    """Type of audit log action"""
    # GL Account changes
    ACCOUNT_CREATED = "account_created"
    ACCOUNT_UPDATED = "account_updated"
    ACCOUNT_DEACTIVATED = "account_deactivated"
    BALANCE_UPDATED = "balance_updated"
    
    # Journal Entry changes
    ENTRY_CREATED = "entry_created"
    ENTRY_UPDATED = "entry_updated"
    ENTRY_POSTED = "entry_posted"
    ENTRY_REVERSED = "entry_reversed"
    ENTRY_DELETED = "entry_deleted"
    ENTRY_REJECTED = "entry_rejected"
    
    # Expense changes
    EXPENSE_CREATED = "expense_created"
    EXPENSE_UPDATED = "expense_updated"
    EXPENSE_SUBMITTED = "expense_submitted"
    EXPENSE_APPROVED = "expense_approved"
    EXPENSE_POSTED = "expense_posted"
    EXPENSE_REJECTED = "expense_rejected"
    EXPENSE_PAYMENT_RECORDED = "expense_payment_recorded"
    
    # Period changes
    PERIOD_CREATED = "period_created"
    PERIOD_LOCKED = "period_locked"
    PERIOD_CLOSED = "period_closed"
    
    # System actions
    RECONCILIATION_COMPLETED = "reconciliation_completed"
    OPENING_BALANCE_IMPORTED = "opening_balance_imported"

    # Payroll changes — previously payroll had zero audit-log coverage
    # despite being one of the most sensitive financial data flows in the
    # system (every other finance-sensitive flow this session got this).
    PAYROLL_GENERATED = "payroll_generated"
    PAYROLL_APPROVED = "payroll_approved"
    PAYROLL_REJECTED = "payroll_rejected"
    PAYROLL_POSTED = "payroll_posted"
    PAYROLL_VOIDED = "payroll_voided"
    PAYROLL_DISBURSED = "payroll_disbursed"


class AuditEntityType(str, Enum):
    """Type of entity being audited"""
    GL_ACCOUNT = "gl_account"
    JOURNAL_ENTRY = "journal_entry"
    EXPENSE = "expense"
    FISCAL_PERIOD = "fiscal_period"
    BANK_RECONCILIATION = "bank_reconciliation"
    PAYROLL_RUN = "payroll_run"


class GLAuditLog(SQLModel, table=True):
    """Complete audit trail of all GL changes
    
    Every GL operation is logged:
    - WHO made the change (user_id)
    - WHAT changed (entity_type, entity_id, action)
    - WHEN it happened (timestamp)
    - WHERE it happened (ip_address)
    - HOW it changed (old_values, new_values)
    - WHY it happened (change_summary, notes)
    
    All audit logs are immutable - they can only be created, never deleted or modified.
    """
    __tablename__ = "gl_audit_logs"
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    
    # WHAT changed
    entity_type: AuditEntityType = Field(
        sa_column=Column(
            SQLEnum(AuditEntityType, name="auditentitytype", values_callable=lambda x: [e.value for e in x]),
            index=True
        )
    )
    entity_id: str = Field(index=True)  # ID of the entity (JE ID, GL account ID, etc)
    action: AuditActionType = Field(
        sa_column=Column(
            SQLEnum(AuditActionType, name="auditactiontype", values_callable=lambda x: [e.value for e in x]),
            index=True
        )
    )
    
    # WHO made the change
    user_id: str = Field(index=True)  # User performing the action
    user_name: Optional[str] = None  # Username for easier reading
    user_role: Optional[str] = None  # User role (admin, HR, etc)
    
    # WHEN it happened
    timestamp: datetime = Field(default_factory=datetime.utcnow, index=True)
    
    # WHERE it happened
    ip_address: Optional[str] = None  # IP address for security audit
    user_agent: Optional[str] = None  # Browser/client for security audit
    
    # WHAT specifically changed (the values)
    old_values: Optional[str] = None  # JSON serialized old values
    new_values: Optional[str] = None  # JSON serialized new values
    
    # WHY it happened (context)
    change_summary: str  # Human-readable description
    related_entity_id: Optional[str] = None  # Related entity (e.g., JE ID for posting action)
    related_entity_type: Optional[str] = None  # Type of related entity
    notes: Optional[str] = None  # Additional notes
    
    # System metadata
    request_id: Optional[str] = None  # For tracing requests
    batch_id: Optional[str] = None  # For grouping related changes (e.g., period close)
    
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    class Config:
        """Audit logs are immutable - no updates after creation"""
        # Index on commonly searched fields for performance
        pass
    
    @staticmethod
    def serialize_values(values: Optional[Dict[str, Any]]) -> Optional[str]:
        """Serialize dictionary values to JSON string"""
        if values is None:
            return None
        try:
            return json.dumps(values, default=str)
        except (TypeError, ValueError):
            return None
    
    @staticmethod
    def deserialize_values(values_str: Optional[str]) -> Optional[Dict[str, Any]]:
        """Deserialize JSON string back to dictionary"""
        if not values_str:
            return None
        try:
            return json.loads(values_str)
        except (TypeError, ValueError):
            return None


class AuditLogCreate(SQLModel):
    """Model for creating audit log entries"""
    entity_type: AuditEntityType
    entity_id: str
    action: AuditActionType
    user_id: str
    user_name: Optional[str] = None
    user_role: Optional[str] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    old_values: Optional[Dict[str, Any]] = None
    new_values: Optional[Dict[str, Any]] = None
    change_summary: str
    related_entity_id: Optional[str] = None
    related_entity_type: Optional[str] = None
    notes: Optional[str] = None
    request_id: Optional[str] = None
    batch_id: Optional[str] = None


class AuditLogResponse(SQLModel):
    """Response model for audit log queries"""
    id: str
    school_id: str
    entity_type: AuditEntityType
    entity_id: str
    action: AuditActionType
    user_id: str
    user_name: Optional[str]
    user_role: Optional[str]
    timestamp: datetime
    ip_address: Optional[str]
    change_summary: str
    related_entity_id: Optional[str]
    notes: Optional[str]


class AuditTrailResponse(SQLModel):
    """Response for complete audit trail of an entity"""
    entity_type: AuditEntityType
    entity_id: str
    total_changes: int
    logs: list[AuditLogResponse]
