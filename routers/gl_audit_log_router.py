"""API Router for GL Audit Logs

Endpoints for:
- Retrieving immutable audit logs
- Filtering logs by entity, action, user, date range
- Generating audit reports
- Exporting audit trails for compliance
"""
import logging
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta

from models.finance.gl_audit_log import (
    GLAuditLog,
    AuditActionType,
    AuditEntityType,
)
from services.gl_audit_log_service import GLAuditLogService
from dependencies import get_current_school_id
from auth import get_current_user, require_roles
from database import get_session
from models.user import User, UserRole

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/gl-audit-logs", tags=["Audit Logs"])

# The audit trail exposes IP addresses and every user's financial actions —
# treat reading it as sensitively as the GL writes it records.
FINANCE_ADMIN_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR)


# ==================== Retrieve by Entity ====================

@router.get("/entity/{entity_type}/{entity_id}", response_model=list)
async def get_entity_audit_logs(
    entity_type: str,
    entity_id: str,
    limit: int = Query(100, ge=1, le=1000),
    current_user: User = Depends(require_roles(*FINANCE_ADMIN_ROLES)),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> list:
    """Get all audit logs for a specific entity
    
    Args:
        entity_type: Type of entity (GL_ACCOUNT, JOURNAL_ENTRY, EXPENSE, etc.)
        entity_id: ID of the entity
        limit: Maximum number of logs to return
        school_id: School identifier
        session: Database session
        
    Returns:
        List of audit log entries for the entity
        
    Raises:
        HTTPException 400: If entity_type is invalid
    """
    try:
        audit_entity_type = AuditEntityType(entity_type)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid entity type. Must be one of: {', '.join([e.value for e in AuditEntityType])}"
        )
    
    service = GLAuditLogService(session)
    logs = await service.get_logs_for_entity(
        school_id=school_id,
        entity_type=audit_entity_type,
        entity_id=entity_id,
        limit=limit,
    )
    
    return [
        {
            "id": log.id,
            "timestamp": log.timestamp.isoformat(),
            "action": log.action.value,
            "user_id": log.user_id,
            "user_name": log.user_name,
            "user_role": log.user_role,
            "change_summary": log.change_summary,
            "ip_address": log.ip_address,
        }
        for log in logs
    ]


# ==================== Retrieve by Action ====================

@router.get("/action/{action}", response_model=list)
async def get_logs_by_action(
    action: str,
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    current_user: User = Depends(require_roles(*FINANCE_ADMIN_ROLES)),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> list:
    """Get audit logs for a specific action type
    
    Args:
        action: Action type (e.g., ENTRY_POSTED, ACCOUNT_UPDATED)
        start_date: Start date in ISO format (optional)
        end_date: End date in ISO format (optional)
        limit: Maximum number of logs
        school_id: School identifier
        session: Database session
        
    Returns:
        List of audit log entries
        
    Raises:
        HTTPException 400: If action is invalid or dates are invalid
    """
    try:
        audit_action = AuditActionType(action)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid action type. Must be one of: {', '.join([a.value for a in AuditActionType])}"
        )
    
    # Parse dates if provided
    start_dt = None
    end_dt = None
    if start_date:
        try:
            start_dt = datetime.fromisoformat(start_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid start_date format")
    
    if end_date:
        try:
            end_dt = datetime.fromisoformat(end_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid end_date format")
    
    service = GLAuditLogService(session)
    logs = await service.get_logs_by_action(
        school_id=school_id,
        action=audit_action,
        start_date=start_dt,
        end_date=end_dt,
        limit=limit,
    )
    
    return [
        {
            "id": log.id,
            "timestamp": log.timestamp.isoformat(),
            "entity_type": log.entity_type.value,
            "entity_id": log.entity_id,
            "user_id": log.user_id,
            "user_name": log.user_name,
            "change_summary": log.change_summary,
        }
        for log in logs
    ]


# ==================== Retrieve by User ====================

@router.get("/user/{user_id}", response_model=list)
async def get_user_activity_logs(
    user_id: str,
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    current_user: User = Depends(require_roles(*FINANCE_ADMIN_ROLES)),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> list:
    """Get all audit logs for actions performed by a user
    
    Useful for user activity audits and fraud investigation.
    
    Args:
        user_id: User ID to filter by
        start_date: Start date in ISO format (optional)
        end_date: End date in ISO format (optional)
        limit: Maximum number of logs
        school_id: School identifier
        session: Database session
        
    Returns:
        List of audit log entries for the user
    """
    # Parse dates if provided
    start_dt = None
    end_dt = None
    if start_date:
        try:
            start_dt = datetime.fromisoformat(start_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid start_date format")
    
    if end_date:
        try:
            end_dt = datetime.fromisoformat(end_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid end_date format")
    
    service = GLAuditLogService(session)
    logs = await service.get_logs_by_user(
        school_id=school_id,
        user_id=user_id,
        start_date=start_dt,
        end_date=end_dt,
        limit=limit,
    )
    
    return [
        {
            "id": log.id,
            "timestamp": log.timestamp.isoformat(),
            "entity_type": log.entity_type.value,
            "action": log.action.value,
            "change_summary": log.change_summary,
            "ip_address": log.ip_address,
        }
        for log in logs
    ]


# ==================== Retrieve by Date Range ====================

@router.get("/date-range", response_model=list)
async def get_logs_by_date_range(
    start_date: str = Query(...),
    end_date: str = Query(...),
    entity_type: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    limit: int = Query(500, ge=1, le=5000),
    current_user: User = Depends(require_roles(*FINANCE_ADMIN_ROLES)),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> list:
    """Get audit logs within a date range
    
    Useful for period-end audits and compliance procedures.
    
    Args:
        start_date: Start date in ISO format (REQUIRED)
        end_date: End date in ISO format (REQUIRED)
        entity_type: Filter by entity type (optional)
        action: Filter by action type (optional)
        limit: Maximum number of logs
        school_id: School identifier
        session: Database session
        
    Returns:
        List of audit log entries
        
    Raises:
        HTTPException 400: If dates are invalid
    """
    try:
        start_dt = datetime.fromisoformat(start_date)
        end_dt = datetime.fromisoformat(end_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use ISO format (YYYY-MM-DDTHH:MM:SS)")
    
    # Parse optional filters
    entity_type_enum = None
    if entity_type:
        try:
            entity_type_enum = AuditEntityType(entity_type)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid entity_type. Must be one of: {', '.join([e.value for e in AuditEntityType])}"
            )
    
    action_enum = None
    if action:
        try:
            action_enum = AuditActionType(action)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid action. Must be one of: {', '.join([a.value for a in AuditActionType])}"
            )
    
    service = GLAuditLogService(session)
    logs = await service.get_logs_by_date_range(
        school_id=school_id,
        start_date=start_dt,
        end_date=end_dt,
        entity_type=entity_type_enum,
        action=action_enum,
        limit=limit,
    )
    
    return [
        {
            "id": log.id,
            "timestamp": log.timestamp.isoformat(),
            "entity_type": log.entity_type.value,
            "entity_id": log.entity_id,
            "action": log.action.value,
            "user_id": log.user_id,
            "user_name": log.user_name,
            "change_summary": log.change_summary,
            "ip_address": log.ip_address,
        }
        for log in logs
    ]


# ==================== Retrieve by Batch ====================

@router.get("/batch/{batch_id}", response_model=list)
async def get_batch_logs(
    batch_id: str,
    current_user: User = Depends(require_roles(*FINANCE_ADMIN_ROLES)),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> list:
    """Get all logs in a batch
    
    Batches group related actions (e.g., all logs from a period close).
    
    Args:
        batch_id: Batch ID
        school_id: School identifier
        session: Database session
        
    Returns:
        List of audit log entries in the batch
    """
    service = GLAuditLogService(session)
    logs = await service.get_logs_by_batch(school_id, batch_id)
    
    return [
        {
            "id": log.id,
            "timestamp": log.timestamp.isoformat(),
            "action": log.action.value,
            "entity_id": log.entity_id,
            "user_name": log.user_name,
            "change_summary": log.change_summary,
        }
        for log in logs
    ]


# ==================== Analysis ====================

@router.get("/report/summary", response_model=dict)
async def get_audit_summary(
    start_date: str = Query(None),
    end_date: str = Query(None),
    current_user: User = Depends(require_roles(*FINANCE_ADMIN_ROLES)),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Get summary of audit activity within a date range
    
    If dates not provided, defaults to last 30 days.
    Useful for audit reports and compliance documentation.
    
    Args:
        start_date: Start date in ISO format (optional, defaults to 30 days ago)
        end_date: End date in ISO format (optional, defaults to today)
        school_id: School identifier
        session: Database session
        
    Returns:
        Audit activity summary with counts by action, entity type, user, and suspicious IPs
        
    Raises:
        HTTPException 400: If dates are invalid
    """
    # Default to last 30 days if dates not provided
    if not end_date:
        end_dt = datetime.utcnow()
    else:
        try:
            end_dt = datetime.fromisoformat(end_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid end_date format")
    
    if not start_date:
        start_dt = end_dt - timedelta(days=30)
    else:
        try:
            start_dt = datetime.fromisoformat(start_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid start_date format")
    
    service = GLAuditLogService(session)
    return await service.get_audit_summary(school_id, start_dt, end_dt)


@router.get("/report/recent", response_model=list)
async def get_recent_activity(
    hours: int = Query(24, ge=1, le=720),
    limit: int = Query(50, ge=1, le=500),
    current_user: User = Depends(require_roles(*FINANCE_ADMIN_ROLES)),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> list:
    """Get recent GL audit activity (useful for dashboards)
    
    Args:
        hours: Number of hours to look back
        limit: Maximum number of logs
        school_id: School identifier
        session: Database session
        
    Returns:
        List of recent audit log entries
    """
    service = GLAuditLogService(session)
    logs = await service.get_recent_activity(school_id, hours=hours, limit=limit)
    
    return [
        {
            "id": log.id,
            "timestamp": log.timestamp.isoformat(),
            "action": log.action.value,
            "entity_type": log.entity_type.value,
            "user_name": log.user_name,
            "change_summary": log.change_summary,
        }
        for log in logs
    ]


# ==================== Export ====================

@router.get("/export/{entity_type}/{entity_id}", response_model=dict)
async def export_audit_trail(
    entity_type: str,
    entity_id: str,
    format: str = Query("list"),
    current_user: User = Depends(require_roles(*FINANCE_ADMIN_ROLES)),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Export complete audit trail for an entity
    
    Useful for compliance reports and documentation.
    
    Args:
        entity_type: Type of entity
        entity_id: Entity ID
        format: Export format ("list" or "detailed")
        school_id: School identifier
        session: Database session
        
    Returns:
        Audit trail in requested format
        
    Raises:
        HTTPException 400: If entity_type or format is invalid
    """
    try:
        audit_entity_type = AuditEntityType(entity_type)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid entity_type. Must be one of: {', '.join([e.value for e in AuditEntityType])}"
        )
    
    if format not in ["list", "detailed"]:
        raise HTTPException(status_code=400, detail="Format must be 'list' or 'detailed'")
    
    service = GLAuditLogService(session)
    return await service.export_audit_trail(
        school_id=school_id,
        entity_type=audit_entity_type,
        entity_id=entity_id,
        format=format,
    )
