"""SMS routes for sending notifications via USMS"""
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, status, Query
from pydantic import BaseModel
from typing import List, Optional
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
from models.user import User, UserRole
from models.student import Student, Parent, StudentParent
from models.staff import Staff
from models.communication import (
    SMSNotification,
    SendSMSRequest,
    SendFeeSMSRequest,
    SendAttendanceSMSRequest,
    SendAnnouncementSMSRequest,
    Announcement,
    Message,
    MessageType,
)
from auth import get_current_user, require_roles
from services.sms_service import sms_service
from services.broadcaster import broadcaster
from database import get_session
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sms", tags=["SMS"])


class SMSResponse(BaseModel):
    """Response schema for SMS operations"""
    success: bool
    message: Optional[str] = None
    message_ids: Optional[List[str]] = None
    error: Optional[str] = None
    recipients: Optional[int] = None


class SMSHistoryItem(BaseModel):
    """SMS notification history item"""
    id: str
    recipient_phone: str
    recipient_name: Optional[str]
    message: str
    notification_type: str
    status: str
    message_id: Optional[str]
    sent_at: Optional[datetime]
    created_at: datetime


@router.post("/send", response_model=SMSResponse)
async def send_sms(
    request: SendSMSRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_roles(
        UserRole.SUPER_ADMIN, 
        UserRole.SCHOOL_ADMIN
    )),
    session: AsyncSession = Depends(get_session)
):
    """
    Send SMS to multiple recipients (admin only)
    
    SMS is processed in background to avoid blocking the request.
    """
    
    # Validate phone numbers
    valid_phones = [p for p in request.phone_numbers if sms_service.validate_phone_number(p)]
    
    if not valid_phones:
        raise HTTPException(
            status_code=400,
            detail="No valid phone numbers provided"
        )
    
    if len(valid_phones) < len(request.phone_numbers):
        logger.warning(f"Invalid phone numbers filtered out: {len(request.phone_numbers) - len(valid_phones)}")
    
    # Check message length
    if len(request.message) > 160 and request.message_type == "text":
        logger.warning(f"Message exceeds 160 chars: {len(request.message)}")
    
    async def send_task():
        try:
            result = await sms_service.send_sms(
                phone_numbers=valid_phones,
                message=request.message,
                message_type=request.message_type
            )
            
            # Log SMS in database
            status_value = "sent" if result["success"] else "failed"
            message_ids = result.get("message_ids", [])
            
            for idx, phone in enumerate(valid_phones):
                sms_record = SMSNotification(
                    school_id=current_user.school_id,
                    recipient_phone=phone,
                    message=request.message,
                    notification_type="bulk",
                    status=status_value,
                    message_id=message_ids[idx] if idx < len(message_ids) else None,
                    error_message=result.get("error") if not result["success"] else None,
                    sent_at=datetime.utcnow() if result["success"] else None
                )
                session.add(sms_record)
            
            await session.commit()
            logger.info(f"SMS sent to {len(valid_phones)} recipients. Status: {status_value}")
        except Exception as e:
            logger.error(f"Error in send SMS task: {str(e)}")
            raise
    
    background_tasks.add_task(send_task)
    
    return SMSResponse(
        success=True,
        message=f"SMS queued for {len(valid_phones)} recipient(s)",
        recipients=len(valid_phones)
    )


@router.post("/fee-reminder", response_model=SMSResponse)
async def send_fee_reminder_sms(
    request: SendFeeSMSRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_roles(
        UserRole.SUPER_ADMIN,
        UserRole.SCHOOL_ADMIN,
        UserRole.HR
    )),
    session: AsyncSession = Depends(get_session)
):
    """
    Send fee reminder SMS to parent of a student
    
    Fetches student and parent details, then sends personalized SMS.
    """
    
    # Fetch student with parent info
    student_query = select(Student).where(
        Student.id == request.student_id,
        Student.school_id == current_user.school_id
    )
    student_result = await session.execute(student_query)
    student = student_result.scalar_one_or_none()
    
    if not student:
        raise HTTPException(
            status_code=404,
            detail="Student not found"
        )
    
    # Fetch parent through StudentParent junction table
    parent_query = select(Parent).join(
        StudentParent, StudentParent.parent_id == Parent.id
    ).where(
        StudentParent.student_id == student.id
    )
    parent_result = await session.execute(parent_query)
    parent = parent_result.scalars().first()
    
    if not parent or not parent.phone:
        raise HTTPException(
            status_code=400,
            detail="Parent phone number not found"
        )
    
    # Validate phone number
    if not sms_service.validate_phone_number(parent.phone):
        raise HTTPException(
            status_code=400,
            detail="Invalid parent phone number"
        )
    
    # Format phone number to include country code
    formatted_phone = sms_service.format_phone_number(parent.phone)

    # Fee reminders are otherwise a one-way text with no record the parent
    # can act on in-app. When the parent has a portal account, also drop
    # this as a real Message so it shows up in their chat inbox — same
    # content, but now something they can reply to instead of just SMS.
    if parent.user_id:
        reminder_text = (
            f"Dear Parent, {student.first_name} has outstanding fees: "
            f"{request.amount_due}. Due: {request.due_date}. Please remit payment."
        )
        message = Message(
            school_id=current_user.school_id,
            sender_id=current_user.id,
            receiver_id=parent.user_id,
            subject="Fee Reminder",
            content=reminder_text,
            student_id=student.id,
            message_type=MessageType.FEE,
        )
        session.add(message)
        await session.commit()
        try:
            await broadcaster.publish({
                "type": "new_message",
                "school_id": current_user.school_id,
                "sender_id": current_user.id,
                "receiver_id": parent.user_id,
            })
        except Exception:
            logger.exception('Failed to publish new_message event for fee reminder')

    async def send_task():
        try:
            result = await sms_service.send_fee_reminder_sms(
                phone_number=formatted_phone,
                student_name=student.first_name,
                amount_due=request.amount_due,
                due_date=request.due_date
            )
            
            status_value = "sent" if result["success"] else "failed"
            
            sms_record = SMSNotification(
                school_id=current_user.school_id,
                recipient_phone=formatted_phone,
                recipient_name=f"{parent.first_name} {parent.last_name}",
                message=result.get("message", ""),
                notification_type="fee_reminder",
                status=status_value,
                message_id=result.get("message_ids", [None])[0] if result.get("message_ids") else None,
                error_message=result.get("error") if not result["success"] else None,
                sent_at=datetime.utcnow() if result["success"] else None
            )
            
            session.add(sms_record)
            await session.commit()
            logger.info(f"Fee reminder SMS sent to {formatted_phone}. Status: {status_value}")
        except Exception as e:
            logger.error(f"Error in fee reminder SMS task: {str(e)}")
            raise
    
    background_tasks.add_task(send_task)
    
    return SMSResponse(
        success=True,
        message=f"Fee reminder SMS queued for {parent.first_name} {parent.last_name}",
        message_ids=None,
        error=None,
        recipients=1
    )


@router.post("/attendance-alert", response_model=SMSResponse)
async def send_attendance_alert_sms(
    request: SendAttendanceSMSRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_roles(
        UserRole.SUPER_ADMIN,
        UserRole.SCHOOL_ADMIN,
        UserRole.TEACHER
    )),
    session: AsyncSession = Depends(get_session)
):
    """
    Send attendance alert SMS to parent
    
    Notifies parent when student attendance drops below threshold.
    """
    
    # Fetch student
    student_query = select(Student).where(
        Student.id == request.student_id,
        Student.school_id == current_user.school_id
    )
    student_result = await session.execute(student_query)
    student = student_result.scalar_one_or_none()
    
    if not student:
        raise HTTPException(
            status_code=404,
            detail="Student not found"
        )
    
    # Validate phone number
    if not sms_service.validate_phone_number(request.phone_number):
        raise HTTPException(
            status_code=400,
            detail="Invalid phone number"
        )
    
    # Format phone number to include country code
    formatted_phone = sms_service.format_phone_number(request.phone_number)

    # Same in-app copy as fee-reminder above: if this student's parent has
    # a portal account, drop the alert as a real Message too. Resolved via
    # student_id (not request.phone_number) since a raw phone string isn't
    # a reliable key back to a Parent row — formatting varies and numbers
    # can be shared across a household.
    parent_query = select(Parent).join(
        StudentParent, StudentParent.parent_id == Parent.id
    ).where(StudentParent.student_id == student.id)
    parent_result = await session.execute(parent_query)
    parent = parent_result.scalars().first()

    if parent and parent.user_id:
        alert_text = (
            f"Alert: {student.first_name}'s attendance is "
            f"{request.attendance_percentage}%. Please monitor."
        )
        message = Message(
            school_id=current_user.school_id,
            sender_id=current_user.id,
            receiver_id=parent.user_id,
            subject="Attendance Alert",
            content=alert_text,
            student_id=student.id,
            message_type=MessageType.ATTENDANCE,
        )
        session.add(message)
        await session.commit()
        try:
            await broadcaster.publish({
                "type": "new_message",
                "school_id": current_user.school_id,
                "sender_id": current_user.id,
                "receiver_id": parent.user_id,
            })
        except Exception:
            logger.exception('Failed to publish new_message event for attendance alert')

    async def send_task():
        try:
            result = await sms_service.send_attendance_sms(
                phone_number=formatted_phone,
                student_name=student.first_name,
                attendance_percentage=request.attendance_percentage
            )
            
            status_value = "sent" if result["success"] else "failed"
            
            sms_record = SMSNotification(
                school_id=current_user.school_id,
                recipient_phone=formatted_phone,
                recipient_name=student.first_name,
                message="",
                notification_type="attendance",
                status=status_value,
                message_id=result.get("message_ids", [None])[0] if result.get("message_ids") else None,
                error_message=result.get("error") if not result["success"] else None,
                sent_at=datetime.utcnow() if result["success"] else None
            )
            
            session.add(sms_record)
            await session.commit()
            logger.info(f"Attendance alert SMS sent to {formatted_phone}. Status: {status_value}")
        except Exception as e:
            logger.error(f"Error in attendance SMS task: {str(e)}")
            raise
    
    background_tasks.add_task(send_task)
    
    return SMSResponse(
        success=True,
        message="Attendance alert SMS queued",
        message_ids=None,
        error=None,
        recipients=1
    )


@router.get("/recipients")
async def get_announcement_recipients(
    announcement_id: str = Query(..., description="Announcement ID to fetch recipients for"),
    current_user: User = Depends(require_roles(
        UserRole.SUPER_ADMIN,
        UserRole.SCHOOL_ADMIN
    )),
    session: AsyncSession = Depends(get_session)
):
    """
    Get recipient phone numbers for an announcement based on audience type
    
    Returns phone numbers for:
    - 'all': All parents, teachers, and students
    - 'parents': All parents only
    - 'teachers': All teachers only
    - 'students': All students only
    - 'specific_class': Students in the target class
    """
    
    # Fetch announcement
    announcement = await session.get(Announcement, announcement_id)
    if not announcement:
        raise HTTPException(
            status_code=404,
            detail="Announcement not found"
        )
    
    # Verify user's school matches
    if announcement.school_id != current_user.school_id:
        raise HTTPException(
            status_code=403,
            detail="Not authorized to view this announcement"
        )
    
    phone_numbers = []
    
    try:
        if announcement.audience == "parents":
            # Get all parent phone numbers for the school
            result = await session.execute(
                select(Parent.phone).where(
                    Parent.school_id == current_user.school_id
                ).distinct()
            )
            parents = result.scalars().all()
            phone_numbers = [p for p in parents if p]
            
        elif announcement.audience == "teachers":
            # Get all teacher phone numbers
            result = await session.execute(
                select(Staff.phone).where(
                    (Staff.school_id == current_user.school_id) &
                    (Staff.staff_type == "teaching") &
                    (Staff.status == "active")
                ).distinct()
            )
            teachers = result.scalars().all()
            phone_numbers = [p for p in teachers if p]
            
        elif announcement.audience == "students":
            # Get all student phone numbers (using parent contact)
            result = await session.execute(
                select(Parent.phone).distinct().where(
                    Parent.id.in_(
                        select(StudentParent.parent_id).where(
                            StudentParent.student_id.in_(
                                select(Student.id).where(
                                    Student.school_id == current_user.school_id
                                )
                            )
                        )
                    )
                )
            )
            contacts = result.scalars().all()
            phone_numbers = [p for p in contacts if p]
            
        elif announcement.audience == "specific_class":
            # Get parent phone numbers for students in specific class
            if not announcement.target_class_id:
                raise HTTPException(
                    status_code=400,
                    detail="Target class required for class-specific announcements"
                )
            result = await session.execute(
                select(Parent.phone).distinct().where(
                    Parent.id.in_(
                        select(StudentParent.parent_id).where(
                            StudentParent.student_id.in_(
                                select(Student.id).where(
                                    (Student.school_id == current_user.school_id) &
                                    (Student.class_id == announcement.target_class_id)
                                )
                            )
                        )
                    )
                )
            )
            contacts = result.scalars().all()
            phone_numbers = [p for p in contacts if p]
            
        elif announcement.audience == "all":
            # Get all parents and teachers
            parent_result = await session.execute(
                select(Parent.phone).where(
                    Parent.school_id == current_user.school_id
                ).distinct()
            )
            parents = parent_result.scalars().all()
            
            teacher_result = await session.execute(
                select(Staff.phone).where(
                    (Staff.school_id == current_user.school_id) &
                    (Staff.staff_type == "teaching") &
                    (Staff.status == "active")
                ).distinct()
            )
            teachers = teacher_result.scalars().all()
            
            phone_numbers = [p for p in parents if p] + [p for p in teachers if p]
        
        # Remove duplicates while preserving order
        seen = set()
        unique_phones = []
        for phone in phone_numbers:
            if phone not in seen:
                seen.add(phone)
                unique_phones.append(phone)
        
        return {
            "phone_numbers": unique_phones,
            "count": len(unique_phones),
            "audience": announcement.audience
        }
        
    except Exception as e:
        logger.error(f"Error fetching announcement recipients: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail="Failed to fetch recipient list"
        )


@router.post("/announcement", response_model=SMSResponse)
async def send_announcement_sms(
    request: SendAnnouncementSMSRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_roles(
        UserRole.SUPER_ADMIN,
        UserRole.SCHOOL_ADMIN
    )),
    session: AsyncSession = Depends(get_session)
):
    """
    Send announcement SMS to multiple recipients (with batch processing)
    
    Sends bulk announcement notification via SMS in batches to handle large contact lists.
    Batch size: 100 recipients per API call
    """
    
    # Validate and format phone numbers
    valid_phones = [
        sms_service.format_phone_number(p) 
        for p in request.phone_numbers 
        if sms_service.validate_phone_number(p)
    ]
    
    if not valid_phones:
        raise HTTPException(
            status_code=400,
            detail="No valid phone numbers provided"
        )
    
    # Batch processing constants
    BATCH_SIZE = 100
    total_batches = (len(valid_phones) + BATCH_SIZE - 1) // BATCH_SIZE
    
    async def send_task():
        try:
            total_sent = 0
            total_failed = 0
            all_results = []
            
            # Process phone numbers in batches
            for batch_num in range(total_batches):
                start_idx = batch_num * BATCH_SIZE
                end_idx = start_idx + BATCH_SIZE
                batch_phones = valid_phones[start_idx:end_idx]
                
                logger.info(f"Processing batch {batch_num + 1}/{total_batches} with {len(batch_phones)} recipients")
                
                try:
                    # Send batch to USMS API
                    result = await sms_service.send_announcement_sms(
                        phone_numbers=batch_phones,
                        announcement_title=request.title,
                        announcement_snippet=request.snippet
                    )
                    
                    all_results.append(result)
                    status_value = "sent" if result["success"] else "failed"
                    message_ids = result.get("message_ids", [])
                    
                    if result["success"]:
                        total_sent += len(batch_phones)
                    else:
                        total_failed += len(batch_phones)
                        logger.error(f"Batch {batch_num + 1} failed: {result.get('error', 'Unknown error')}")
                    
                    # Save records for this batch
                    for idx, phone in enumerate(batch_phones):
                        sms_record = SMSNotification(
                            school_id=current_user.school_id,
                            recipient_phone=phone,
                            message=f"Announcement: {request.title}",
                            notification_type="announcement",
                            status=status_value,
                            message_id=message_ids[idx] if idx < len(message_ids) else None,
                            error_message=result.get("error") if not result["success"] else None,
                            sent_at=datetime.utcnow() if result["success"] else None
                        )
                        session.add(sms_record)
                    
                    # Commit batch records
                    await session.commit()
                    logger.info(f"Batch {batch_num + 1}/{total_batches} completed. Status: {status_value} ({len(batch_phones)} recipients)")
                    
                except Exception as batch_error:
                    logger.error(f"Error processing batch {batch_num + 1}: {str(batch_error)}")
                    total_failed += len(batch_phones)
                    # Mark batch as failed in DB
                    for phone in batch_phones:
                        sms_record = SMSNotification(
                            school_id=current_user.school_id,
                            recipient_phone=phone,
                            message=f"Announcement: {request.title}",
                            notification_type="announcement",
                            status="failed",
                            error_message=str(batch_error)
                        )
                        session.add(sms_record)
                    await session.commit()
            
            # Log final summary
            logger.info(f"Announcement SMS batch processing complete. Total sent: {total_sent}, Total failed: {total_failed}, Total batches: {total_batches}")
            
        except Exception as e:
            logger.error(f"Critical error in announcement SMS task: {str(e)}")
            raise
    
    background_tasks.add_task(send_task)
    
    return SMSResponse(
        success=True,
        message=f"Announcement SMS queued for {len(valid_phones)} recipient(s)",
        message_ids=None,
        error=None,
        recipients=len(valid_phones)
    )


@router.get("/balance", response_model=dict)
async def check_sms_balance(
    current_user: User = Depends(require_roles(
        UserRole.SUPER_ADMIN,
        UserRole.SCHOOL_ADMIN
    ))
):
    """
    Check SMS account balance
    
    Returns current SMS credits balance.
    """
    balance = await sms_service.check_sms_balance()
    return balance


@router.get("/notifications", response_model=dict)
async def get_sms_notifications(
    current_user: User = Depends(require_roles(
        UserRole.SUPER_ADMIN,
        UserRole.SCHOOL_ADMIN
    )),
    session: AsyncSession = Depends(get_session),
    status_filter: Optional[str] = Query(None, description="Filter by status: pending, sent, failed, delivered"),
    notification_type: Optional[str] = Query(None, description="Filter by type: fee_reminder, attendance, announcement, bulk, general"),
    limit: int = Query(50, ge=1, le=500),
    page: int = Query(1, ge=1)
):
    """
    Get SMS notification history
    
    Returns list of SMS notifications with optional filtering.
    """
    query = select(SMSNotification).where(
        SMSNotification.school_id == current_user.school_id
    )
    
    # Apply filters
    if status_filter:
        query = query.where(SMSNotification.status == status_filter)
    
    if notification_type:
        query = query.where(SMSNotification.notification_type == notification_type)
    
    # Get total count
    count_query = select(func.count(SMSNotification.id)).where(
        SMSNotification.school_id == current_user.school_id
    )
    if status_filter:
        count_query = count_query.where(SMSNotification.status == status_filter)
    if notification_type:
        count_query = count_query.where(SMSNotification.notification_type == notification_type)
    
    count_result = await session.execute(count_query)
    total = count_result.scalar()
    
    # Pagination
    offset = (page - 1) * limit
    query = query.order_by(SMSNotification.created_at.desc()).offset(offset).limit(limit)
    
    result = await session.execute(query)
    notifications = result.scalars().all()
    
    return {
        "success": True,
        "total": total,
        "page": page,
        "limit": limit,
        "count": len(notifications),
        "notifications": [
            {
                "id": n.id,
                "recipient_phone": n.recipient_phone,
                "recipient_name": n.recipient_name,
                "message": n.message,
                "notification_type": n.notification_type,
                "status": n.status,
                "message_id": n.message_id,
                "sent_at": n.sent_at,
                "created_at": n.created_at
            }
            for n in notifications
        ]
    }


@router.get("/notifications/{notification_id}", response_model=dict)
async def get_sms_notification(
    notification_id: str,
    current_user: User = Depends(require_roles(
        UserRole.SUPER_ADMIN,
        UserRole.SCHOOL_ADMIN
    )),
    session: AsyncSession = Depends(get_session)
):
    """
    Get a specific SMS notification by ID
    
    Returns detailed information about a single SMS notification.
    """
    query = select(SMSNotification).where(
        SMSNotification.id == notification_id,
        SMSNotification.school_id == current_user.school_id
    )
    
    result = await session.execute(query)
    notification = result.scalar_one_or_none()
    
    if not notification:
        raise HTTPException(
            status_code=404,
            detail="SMS notification not found"
        )
    
    return {
        "success": True,
        "notification": {
            "id": notification.id,
            "recipient_phone": notification.recipient_phone,
            "recipient_name": notification.recipient_name,
            "message": notification.message,
            "notification_type": notification.notification_type,
            "status": notification.status,
            "message_id": notification.message_id,
            "error_message": notification.error_message,
            "sent_at": notification.sent_at,
            "created_at": notification.created_at,
            "updated_at": notification.updated_at
        }
    }


@router.get("/statistics", response_model=dict)
async def get_sms_statistics(
    current_user: User = Depends(require_roles(
        UserRole.SUPER_ADMIN,
        UserRole.SCHOOL_ADMIN
    )),
    session: AsyncSession = Depends(get_session)
):
    """
    Get SMS statistics for the school
    
    Returns overall SMS usage statistics and health.
    """
    school_id = current_user.school_id
    
    # Total notifications
    total_query = select(func.count(SMSNotification.id)).where(
        SMSNotification.school_id == school_id
    )
    total_result = await session.execute(total_query)
    total_count = total_result.scalar() or 0
    
    # Sent notifications
    sent_query = select(func.count(SMSNotification.id)).where(
        SMSNotification.school_id == school_id,
        SMSNotification.status == "sent"
    )
    sent_result = await session.execute(sent_query)
    sent_count = sent_result.scalar() or 0
    
    # Failed notifications
    failed_query = select(func.count(SMSNotification.id)).where(
        SMSNotification.school_id == school_id,
        SMSNotification.status == "failed"
    )
    failed_result = await session.execute(failed_query)
    failed_count = failed_result.scalar() or 0
    
    # By type
    type_query = select(
        SMSNotification.notification_type,
        func.count(SMSNotification.id)
    ).where(
        SMSNotification.school_id == school_id
    ).group_by(SMSNotification.notification_type)
    
    type_result = await session.execute(type_query)
    type_stats = dict(type_result.all())
    
    success_rate = (sent_count / total_count * 100) if total_count > 0 else 0
    
    return {
        "success": True,
        "statistics": {
            "total": total_count,
            "sent": sent_count,
            "failed": failed_count,
            "pending": total_count - sent_count - failed_count,
            "success_rate": round(success_rate, 2),
            "by_type": type_stats
        }
    }
