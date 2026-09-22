"""Ticket management routes"""
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from models.ticket import (
    Ticket, TicketCreate, TicketUpdate, TicketResponse, TicketDetailResponse,
    TicketComment, TicketCommentCreate, TicketCommentResponse, TicketCloseRequest,
    TicketStatus
)
from models.user import User, UserRole
from models.school import School
from database import get_session
from auth import get_current_user, require_roles
from services.ticket_service import TicketService
from services.notification_service import SMSNotificationService
from config import get_settings

router = APIRouter(prefix="/tickets", tags=["Tickets"])
settings = get_settings()


def get_notification_service():
    """Get SMS notification service for tickets"""
    return SMSNotificationService()


@router.post("/", response_model=dict, status_code=201)
async def create_ticket(
    ticket_data: TicketCreate,
    current_user: User = Depends(require_roles(UserRole.SCHOOL_ADMIN, UserRole.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Create a new support ticket"""
    
    if not current_user.school_id and current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="No school context")
    
    school_id = current_user.school_id
    
    # Create ticket
    ticket = await TicketService.create_ticket(
        session,
        school_id,
        current_user.id,
        ticket_data
    )
    
    # Send SMS notification to super admin
    try:
        notification_service = get_notification_service()
        if notification_service:
            superadmin_phone = settings.superadmin_whatsapp_phone
        
        # Get school name
        school_query = __import__('sqlmodel').select(School).where(School.id == school_id)
        school_result = await session.execute(school_query)
        school = school_result.scalar_one_or_none()
        school_name = school.name if school else "Unknown School"
        
        sms_result = await notification_service.send_ticket_created(
            school_name=school_name,
            ticket_title=ticket.title,
            ticket_priority=ticket.priority.value,
            ticket_id=ticket.id,
            superadmin_phone=superadmin_phone
        )
        
        # Log notification
        if sms_result.get("success"):
            await TicketService.log_notification(
                session,
                ticket.id,
                superadmin_phone,
                "sms",
                sms_result.get("status", "sent"),
                sms_result.get("message_id")
            )
    except Exception as e:
        # Log error but don't fail the request
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Failed to send SMS notification: {str(e)}")
    
    # Convert ticket to response model
    ticket_response = TicketResponse(
        id=ticket.id,
        school_id=ticket.school_id,
        title=ticket.title,
        description=ticket.description,
        category=ticket.category,
        priority=ticket.priority,
        status=ticket.status,
        created_by_id=ticket.created_by_id,
        assigned_to_id=ticket.assigned_to_id,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
        closed_at=ticket.closed_at,
        resolution_summary=ticket.resolution_summary
    )
    
    return {
        "success": True,
        "data": ticket_response,
        "message": "Ticket created successfully and notification sent"
    }


@router.get("/", response_model=dict)
async def list_tickets(
    status: Optional[str] = None,
    category: Optional[str] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """List tickets - school admin sees own, super admin sees all"""
    
    if current_user.role == UserRole.SUPER_ADMIN:
        tickets, total = await TicketService.get_all_tickets(
            session,
            status=status,
            page=page,
            limit=limit
        )
    elif current_user.role == UserRole.SCHOOL_ADMIN:
        if not current_user.school_id:
            raise HTTPException(status_code=403, detail="No school context")
        
        tickets, total = await TicketService.get_school_tickets(
            session,
            school_id=current_user.school_id,
            status=status,
            category=category,
            page=page,
            limit=limit
        )
    else:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    return {
        "success": True,
        "data": [
            TicketResponse(
                id=t.id,
                school_id=t.school_id,
                title=t.title,
                description=t.description,
                category=t.category,
                priority=t.priority,
                status=t.status,
                created_by_id=t.created_by_id,
                assigned_to_id=t.assigned_to_id,
                created_at=t.created_at,
                updated_at=t.updated_at,
                closed_at=t.closed_at,
                resolution_summary=t.resolution_summary
            ).dict()
            for t in tickets
        ],
        "total": total,
        "page": page,
        "limit": limit,
        "pages": (total + limit - 1) // limit
    }


@router.get("/{ticket_id}", response_model=dict)
async def get_ticket_detail(
    ticket_id: str,
    current_user: User = Depends(require_roles(UserRole.SCHOOL_ADMIN, UserRole.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Get ticket details with comments"""
    
    ticket = await TicketService.get_ticket_by_id(session, ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    
    # Check authorization
    if (current_user.role != UserRole.SUPER_ADMIN and 
        ticket.school_id != current_user.school_id):
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # Get comments
    comments = await TicketService.get_ticket_comments(session, ticket_id)
    
    # Convert comments to response models
    comment_responses = [
        TicketCommentResponse(
            id=c.id,
            ticket_id=c.ticket_id,
            author_id=c.author_id,
            comment=c.comment,
            is_internal=c.is_internal,
            created_at=c.created_at
        ).dict()
        for c in comments
    ]
    
    # Create ticket detail response
    ticket_detail = TicketDetailResponse(
        id=ticket.id,
        school_id=ticket.school_id,
        title=ticket.title,
        description=ticket.description,
        category=ticket.category,
        priority=ticket.priority,
        status=ticket.status,
        created_by_id=ticket.created_by_id,
        assigned_to_id=ticket.assigned_to_id,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
        closed_at=ticket.closed_at,
        resolution_summary=ticket.resolution_summary,
        comments=comment_responses
    )
    
    return {
        "success": True,
        "data": ticket_detail.dict()
    }


@router.put("/{ticket_id}/status", response_model=dict)
async def update_ticket_status(
    ticket_id: str,
    update_data: TicketUpdate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Update ticket status (super admin only)"""
    
    ticket = await TicketService.get_ticket_by_id(session, ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    
    # Update status
    updated_ticket = await TicketService.update_ticket_status(
        session,
        ticket_id,
        update_data.status,
        update_data.assigned_to_id
    )
    
    # Send SMS update to school admin
    try:
        notification_service = get_notification_service()
        
        # Get school and creator info
        school_query = __import__('sqlmodel').select(School).where(School.id == ticket.school_id)
        school_result = await session.execute(school_query)
        school = school_result.scalar_one_or_none()
        school_name = school.name if school else "Unknown School"
        
        user_query = __import__('sqlmodel').select(User).where(User.id == ticket.created_by_id)
        user_result = await session.execute(user_query)
        creator = user_result.scalar_one_or_none()
        creator_phone = creator.phone if creator and creator.phone else None
        
        if creator_phone and notification_service:
            sms_result = await notification_service.send_ticket_status_update(
                school_name=school_name,
                ticket_title=ticket.title,
                new_status=update_data.status.value,
                school_admin_phone=creator_phone
            )
            
            if sms_result.get("success"):
                await TicketService.log_notification(
                    session,
                    ticket_id,
                    creator_phone,
                    "sms",
                    sms_result.get("status", "sent"),
                    sms_result.get("message_id")
                )
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Failed to send status update: {str(e)}")
    
    # Convert to response model
    response = TicketResponse(
        id=updated_ticket.id,
        school_id=updated_ticket.school_id,
        title=updated_ticket.title,
        description=updated_ticket.description,
        category=updated_ticket.category,
        priority=updated_ticket.priority,
        status=updated_ticket.status,
        created_by_id=updated_ticket.created_by_id,
        assigned_to_id=updated_ticket.assigned_to_id,
        created_at=updated_ticket.created_at,
        updated_at=updated_ticket.updated_at,
        closed_at=updated_ticket.closed_at,
        resolution_summary=updated_ticket.resolution_summary
    )
    
    return {
        "success": True,
        "data": response.dict(),
        "message": "Ticket updated and notification sent"
    }


@router.post("/{ticket_id}/comments", response_model=dict, status_code=201)
async def add_ticket_comment(
    ticket_id: str,
    comment_data: TicketCommentCreate,
    current_user: User = Depends(require_roles(UserRole.SCHOOL_ADMIN, UserRole.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Add comment to ticket"""
    
    ticket = await TicketService.get_ticket_by_id(session, ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    
    # Check authorization
    if (current_user.role != UserRole.SUPER_ADMIN and 
        ticket.school_id != current_user.school_id):
        raise HTTPException(status_code=403, detail="Not authorized")
    
    comment = await TicketService.add_comment(
        session,
        ticket_id,
        current_user.id,
        comment_data.comment,
        comment_data.is_internal
    )
    
    if not comment:
        raise HTTPException(status_code=404, detail="Failed to add comment")
    
    # Convert to response model
    comment_response = TicketCommentResponse(
        id=comment.id,
        ticket_id=comment.ticket_id,
        author_id=comment.author_id,
        comment=comment.comment,
        is_internal=comment.is_internal,
        created_at=comment.created_at
    )
    
    return {
        "success": True,
        "data": comment_response.dict(),
        "message": "Comment added successfully"
    }


@router.post("/{ticket_id}/close", response_model=dict)
async def close_ticket(
    ticket_id: str,
    close_data: TicketCloseRequest,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Close ticket with resolution"""
    
    ticket = await TicketService.get_ticket_by_id(session, ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    
    # Close ticket
    closed_ticket = await TicketService.close_ticket(
        session,
        ticket_id,
        close_data.resolution_summary
    )
    
    # Send SMS closure notification
    try:
        notification_service = get_notification_service()
        
        # Get school and creator info
        school_query = __import__('sqlmodel').select(School).where(School.id == ticket.school_id)
        school_result = await session.execute(school_query)
        school = school_result.scalar_one_or_none()
        school_name = school.name if school else "Unknown School"
        
        user_query = __import__('sqlmodel').select(User).where(User.id == ticket.created_by_id)
        user_result = await session.execute(user_query)
        creator = user_result.scalar_one_or_none()
        creator_phone = creator.phone if creator and creator.phone else None
        
        if creator_phone and notification_service:
            sms_result = await notification_service.send_ticket_closed(
                school_name=school_name,
                ticket_title=ticket.title,
                resolution=close_data.resolution_summary,
                school_admin_phone=creator_phone
            )
            
            if sms_result.get("success"):
                await TicketService.log_notification(
                    session,
                    ticket_id,
                    creator_phone,
                    "sms",
                    sms_result.get("status", "sent"),
                    sms_result.get("message_id")
                )
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Failed to send closure notification: {str(e)}")
    
    # Convert to response model
    response = TicketResponse(
        id=closed_ticket.id,
        school_id=closed_ticket.school_id,
        title=closed_ticket.title,
        description=closed_ticket.description,
        category=closed_ticket.category,
        priority=closed_ticket.priority,
        status=closed_ticket.status,
        created_by_id=closed_ticket.created_by_id,
        assigned_to_id=closed_ticket.assigned_to_id,
        created_at=closed_ticket.created_at,
        updated_at=closed_ticket.updated_at,
        closed_at=closed_ticket.closed_at,
        resolution_summary=closed_ticket.resolution_summary
    )
    
    return {
        "success": True,
        "data": response.dict(),
        "message": "Ticket closed and notification sent"
    }


@router.get("/stats/overview", response_model=dict)
async def get_ticket_stats(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get ticket statistics"""
    
    if current_user.role == UserRole.SUPER_ADMIN:
        stats = await TicketService.get_ticket_statistics(session)
    elif current_user.role == UserRole.SCHOOL_ADMIN:
        stats = await TicketService.get_ticket_statistics(session, current_user.school_id)
    else:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    return {
        "success": True,
        "data": stats
    }
